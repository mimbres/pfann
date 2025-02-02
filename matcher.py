import csv
import math
import os
import sys
import warnings

import faiss
import julius
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.multiprocessing as mp
import torchvision
import tensorboardX
import tqdm

# torchaudio currently (0.7) will throw warning that cannot be disabled
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import torchaudio

import simpleutils
from model import FpNetwork
from datautil.musicdata import MusicDataset

if __name__ == "__main__":
    mp.set_start_method('spawn')
    if len(sys.argv) < 4:
        print('Usage: python %s <query list> <database dir> <result file>' % sys.argv[0])
        sys.exit()
    file_list_for_query = sys.argv[1]
    dir_for_db = sys.argv[2]
    result_file = sys.argv[3]
    result_file2 = os.path.splitext(result_file) # for more detailed output
    result_file2 = result_file2[0] + '_detail.csv'
    result_file_score = result_file + '.bin'
    configs = os.path.join(dir_for_db, 'configs.json')
    params = simpleutils.read_config(configs)
    
    visualize = False

    d = params['model']['d']
    h = params['model']['h']
    u = params['model']['u']
    F_bin = params['n_mels']
    segn = int(params['segment_size'] * params['sample_rate'])
    T = (segn + params['stft_hop'] - 1) // params['stft_hop']
    
    top_k = params['indexer']['top_k']
    frame_shift_mul = params['indexer'].get('frame_shift_mul', 1)

    print('loading model...')
    device = torch.device('cuda')
    model = FpNetwork(d, h, u, F_bin, T, params['model']).to(device)
    model.load_state_dict(torch.load(os.path.join(dir_for_db, 'model.pt')))
    print('model loaded')
    
    print('loading database...')
    with open(os.path.join(dir_for_db, 'songList.txt'), 'r', encoding='utf8') as fin:
        songList = []
        for line in fin:
            if line.endswith('\n'): line = line[:-1]
            songList.append(line)
    
    landmarkKey = np.fromfile(os.path.join(dir_for_db, 'landmarkKey'), dtype=np.int32)
    index = faiss.read_index(os.path.join(dir_for_db, 'landmarkValue'))
    index.make_direct_map()
    assert len(songList) == landmarkKey.shape[0]
    index2song = np.repeat(np.arange(len(songList)), landmarkKey)
    landmarkKey = np.pad(np.cumsum(landmarkKey, dtype=np.int64), (1, 0))
    print('database loaded')
    if isinstance(index, faiss.IndexIVF):
        print('inverse list count:', index.nlist)
        index.nprobe = params['indexer'].get('nprobe', 50)
        print('num probes:', index.nprobe)

    # doing inference, turn off gradient
    model.eval()
    for param in model.parameters():
        param.requires_grad = False

    dataset = MusicDataset(file_list_for_query, params)
    # no task parallelism
    loader = DataLoader(dataset, num_workers=0)
    
    mel = torchaudio.transforms.MelSpectrogram(
        sample_rate=params['sample_rate'],
        n_fft=params['stft_n'],
        hop_length=params['stft_hop'],
        f_min=params['f_min'],
        f_max=params['f_max'],
        n_mels=params['n_mels'],
        window_fn=torch.hann_window).to(device)
    
    fout = open(result_file, 'w', encoding='utf8', newline='\n')
    fout2 = open(result_file2, 'w', encoding='utf8', newline='\n')
    fout_score = open(result_file_score, 'wb')
    detail_writer = csv.writer(fout2)
    detail_writer.writerow(['query', 'answer', 'score', 'time', 'part_scores'])
    for dat in tqdm.tqdm(loader):
        embeddings = []
        grads = []
        specs = []
        i, name, wav = dat
        i = int(i) # i is leaking file handles!
        # batch size should be less than 20 because query contains at most 19 segments
        for batch in DataLoader(wav.squeeze(0), batch_size=16):
            g = batch.to(device)
            
            # Mel spectrogram
            with warnings.catch_warnings():
                # torchaudio is still using deprecated function torch.rfft
                warnings.simplefilter("ignore")
                g = mel(g)
            g = torch.log(g + 1e-12)
            if params.get('spec_norm', 'l2') == 'max':
                g -= torch.amax(g, dim=(1,2)).reshape(-1, 1, 1)
            if visualize:
                g.requires_grad = True
            z = model.forward(g, norm=False).cpu()
            if visualize:
                z.backward(z)
                z.detach_()
                grads.append(g.grad.cpu())
                specs.append(g.detach().cpu())
            z = torch.nn.functional.normalize(z, p=2)
            embeddings.append(z)
        embeddings = torch.cat(embeddings)
        if visualize:
            grads = torch.cat(grads)
            specs = torch.cat(specs)
        song_score = np.zeros(len(songList), dtype=np.float32)
        if top_k == -1:
            # optimize for exhaustive search
            arr = faiss.vector_to_array(index.xb).reshape([index.ntotal, d])
            dists = embeddings.numpy() @ arr.T
            query_len = embeddings.shape[0]
            scoreboard = np.zeros(index.ntotal + len(songList) * query_len)
            shift = index2song * query_len + np.arange(index.ntotal)
            for t in range(query_len):
                scoreboard[shift + (query_len - t)] += dists[t]
            t1_s = np.argmax(scoreboard)
            sco = scoreboard[t1_s]
            t1 = np.searchsorted(shift, t1_s, side='right') - 1
            ans = index2song[t1]
            t0 = landmarkKey[ans]
            t0_s = shift[t0]
            tim = t1_s - t0_s - query_len
            upsco = []
            for t in range(query_len):
                t2 = t0 + tim + t
                if t0 <= t2 < int(landmarkKey[ans+1]):
                    upsco.append(float(dists[t, t2]))
            for songId in range(len(songList)):
                lo = landmarkKey[songId] + songId * query_len
                hi = landmarkKey[songId+1] + (songId+1) * query_len
                song_score[songId] = np.max(scoreboard[lo:hi])
        else:
            dists, ids = index.search(x=embeddings.numpy(), k=top_k)
            scoreboard = {}
            upcount = {}
            for t in range(ids.shape[0]):
                if np.all(dists[t] <= 2):
                    last_k = top_k
                else:
                    last_k = np.argmax(dists[t] > 2)
                for j in range(last_k):
                    t1 = int(ids[t, j])
                    #songId = int(np.searchsorted(landmarkKey, t1, side='right'))
                    songId = int(index2song[t1])
                    t0 = int(landmarkKey[songId])
                    #dt = t1 - t0 - round(t/frame_shift_mul)
                    dt = (t1 - t0) * frame_shift_mul - t
                    key = (songId, dt)
                    if key in scoreboard:
                        scoreboard[key] += float(dists[t, j])
                        upcount[key] += [float(dists[t, j]), t, j]
                    else:
                        scoreboard[key] = float(dists[t, j])
                        upcount[key] = [float(dists[t, j]), t, j]
            
            if True:
                # sum of whole sequence
                N = frame_shift_mul
                nFrames = embeddings.shape[0]
                queryLen = (nFrames - 1) // N + 1
                xn = np.pad(embeddings.numpy(), [(0, queryLen * N - nFrames), (0,0)])
                xn = xn.reshape([queryLen, N, d]).transpose([1, 0, 2])
                for songId, dt in scoreboard.keys():
                    songStart = int(landmarkKey[songId])
                    songLen = int(landmarkKey[songId+1]) - songStart
                    t_frame = (dt-1)//N + 1
                    t_start = max(t_frame, 0)
                    t_end = min(t_frame + queryLen, songLen)
                    # get song vector
                    vectors = np.stack([index.reconstruct(i) for i in range(songStart + t_start, songStart + t_end)])
                    #vectors = index.reconstruct_n(songStart + t_start, t_end - t_start)
                    q_start = t_start - t_frame
                    q_end = t_end - t_frame
                    # compute sum of segment score
                    sco = np.dot(xn[:, q_start:q_end].reshape([N, -1]), vectors.flatten())
                    scoreboard[songId, dt] = np.max(sco).item()
            
            scoreboard = [(dist,id_) for id_,dist in scoreboard.items()]
            for sco, ans_tim in scoreboard:
                ans = ans_tim[0]
                song_score[ans] = max(song_score[ans], sco)
            sco, (ans, tim) = max(scoreboard)
            upsco = upcount[ans, tim]
        ans = songList[ans]
        tim /= frame_shift_mul
        tim *= params['hop_size']
        if visualize:
            grads = torch.abs(grads)
            grads = torch.nn.functional.normalize(grads, p=np.inf)
            grads = grads.transpose(0, 1).flatten(1, 2)
            grads = grads.repeat(3, 1, 1)
            specs = specs.transpose(0, 1).flatten(1, 2)
            grads[1] = specs - math.log(1e-6)
            grads[1] /= torch.max(grads[1])
            grads[0] = torch.nn.functional.relu(grads[0])
            grads[1] *= 1 - grads[0]
            grads[2] = 0
            grads = torch.flip(grads, [1])
            grads[:,:,::32] = 0
            torchvision.utils.save_image(grads, '%s.png' % os.path.basename(name[0]))
        fout.write('%s\t%s\n' % (name[0], ans))
        fout.flush()
        detail_writer.writerow([name[0], ans, sco, tim] + upsco)
        fout2.flush()
        
        fout_score.write(song_score.tobytes())
    fout.close()
    fout2.close()
else:
    torch.set_num_threads(1)
