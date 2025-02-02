import os
import shutil
import sys
import warnings

import faiss
import julius
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.multiprocessing as mp
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
    if len(sys.argv) < 3:
        print('Usage: python %s <music list file> <db location>' % sys.argv[0])
        sys.exit()
    file_list_for_db = sys.argv[1]
    dir_for_db = sys.argv[2]
    configs = 'configs/default.json'
    if len(sys.argv) >= 4:
        configs = sys.argv[3]
    params = simpleutils.read_config(configs)

    d = params['model']['d']
    h = params['model']['h']
    u = params['model']['u']
    F_bin = params['n_mels']
    segn = int(params['segment_size'] * params['sample_rate'])
    T = (segn + params['stft_hop'] - 1) // params['stft_hop']

    print('loading model...')
    device = torch.device('cuda')
    model = FpNetwork(d, h, u, F_bin, T, params['model']).to(device)
    model.load_state_dict(torch.load(os.path.join(params['model_dir'], 'model.pt')))
    print('model loaded')

    # doing inference, turn off gradient
    model.eval()
    for param in model.parameters():
        param.requires_grad = False

    params['indexer']['frame_shift_mul'] = 1
    dataset = MusicDataset(file_list_for_db, params)
    loader = DataLoader(dataset, num_workers=4)
    
    mel = torchaudio.transforms.MelSpectrogram(
        sample_rate=params['sample_rate'],
        n_fft=params['stft_n'],
        hop_length=params['stft_hop'],
        f_min=params['f_min'],
        f_max=params['f_max'],
        n_mels=params['n_mels'],
        window_fn=torch.hann_window).to(device)
    
    os.makedirs(dir_for_db, exist_ok=True)
    embeddings_file = open(os.path.join(dir_for_db, 'embeddings'), 'wb')
    lbl = []
    landmarkKey = []
    embeddings = 0
    for dat in tqdm.tqdm(loader):
        i, name, wav = dat
        i = int(i) # i is leaking file handles!
        # batch size should be less than 20 because query contains at most 19 segments
        for batch in torch.split(wav.squeeze(0), 16):
            g = batch.to(device)
            
            # Mel spectrogram
            with warnings.catch_warnings():
                # torchaudio is still using deprecated function torch.rfft
                warnings.simplefilter("ignore")
                g = mel(g)
            g = torch.log(g + 1e-12)
            if params.get('spec_norm', 'l2') == 'max':
                g -= torch.amax(g, dim=(1,2)).reshape(-1, 1, 1)
            z = model(g).cpu()
            for _ in z:
                lbl.append(i)
            embeddings_file.write(z.numpy().tobytes())
            embeddings += z.shape[0]
        landmarkKey.append(int(wav.shape[1]))
    embeddings_file.flush()
    print('total', embeddings, 'embeddings')
    #writer = tensorboardX.SummaryWriter()
    #writer.add_embedding(embeddings, lbl)
    
    # train indexer
    print('training indexer')
    index = faiss.index_factory(d, params['indexer']['index_factory'], faiss.METRIC_INNER_PRODUCT)
    
    embeddings = np.fromfile(os.path.join(dir_for_db, 'embeddings'), dtype=np.float32).reshape([-1, d])
    if not index.is_trained:
        index.verbose = True
        index.train(embeddings)
    #index = faiss.IndexFlatIP(d)
    
    # write database
    print('writing database')
    index.add(embeddings)
    faiss.write_index(index, os.path.join(dir_for_db, 'landmarkValue'))
    
    landmarkKey = np.array(landmarkKey, dtype=np.int32)
    landmarkKey.tofile(os.path.join(dir_for_db, 'landmarkKey'))
    
    shutil.copyfile(file_list_for_db, os.path.join(dir_for_db, 'songList.txt'))
    
    # write settings
    shutil.copyfile(configs, os.path.join(dir_for_db, 'configs.json'))
    
    # write model
    shutil.copyfile(os.path.join(params['model_dir'], 'model.pt'),
        os.path.join(dir_for_db, 'model.pt'))
else:
    torch.set_num_threads(1)
