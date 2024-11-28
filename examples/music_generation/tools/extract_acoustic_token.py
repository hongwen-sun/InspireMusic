#!/usr/bin/env python3
# Copyright (c) 2024 Alibaba Inc (authors: Xiang Lyu)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import logging
import torch
from tqdm import tqdm
import onnxruntime
import numpy as np
import torchaudio
from inspiremusic.utils.audio_utils import normalize, split_wav_into_chunks
from inspiremusic.hificodec.vqvae import VQVAE
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main(args):
    audio_min_length = 1.0
    audio_max_length = 30.0
    max_chunk_size = int(args.sample_rate * audio_max_length)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    utt2wav = {}
    with open('{}/wav.scp'.format(args.dir)) as f:
        for l in f:
            l = l.replace('\n', '').split()
            utt2wav[l[0]] = l[1]

    # option = onnxruntime.SessionOptions()
    # option.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    # option.intra_op_num_threads = 1
    # providers = ["CUDAExecutionProvider"]
    # ort_session = onnxruntime.InferenceSession(args.onnx_path, sess_options=option, providers=providers)

    model = VQVAE(args.config_path, args.ckpt_path, with_encoder=True)
    model.cuda()
    model.eval()

    utt2acoustic_token = {}
    start_time = time.time()
    for utt in tqdm(utt2wav.keys()):
        audio, sample_rate = torchaudio.load(utt2wav[utt])
        if sample_rate != args.sample_rate:
            audio = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=args.sample_rate)(audio)
        audio_length = audio.shape[1]
        if audio_length > args.sample_rate * audio_min_length:
            # audio = normalize(audio)
            if audio_length > max_chunk_size:
                wav_chunks = split_wav_into_chunks(audio_length, audio, max_chunk_size)
                for chunk in wav_chunks:
                    chunk = torch.tensor(chunk, dtype=torch.float32).to(device)
                    acoustic_token = model.encode(chunk)
                    if acoustic_token.is_cuda:
                        acoustic_token = acoustic_token.cpu()
                    acoustic_token = acoustic_token.numpy().astype(np.int16)
                    if utt not in utt2acoustic_token.keys():
                        utt2acoustic_token[utt] = acoustic_token
                    else:
                        utt2acoustic_token[utt] = np.concatenate((utt2acoustic_token[utt], acoustic_token), axis=1)
            else:
                audio = torch.tensor(audio, dtype=torch.float32).to(device)
                acoustic_token = model.encode(audio)
                if acoustic_token.is_cuda:
                    acoustic_token = acoustic_token.cpu()
                acoustic_token = acoustic_token.numpy().astype(np.int16)
                utt2acoustic_token[utt] = acoustic_token
        else:
            logging.warning('This audio length is too short.')
        
            # acoustic_token = ort_session.run(None, {ort_session.get_inputs()[0].name: feat.detach().cpu().numpy(),
            #                                       ort_session.get_inputs()[1].name: np.array([feat.shape[2]], dtype=np.int32)})[0].flatten().tolist()
    torch.save(utt2acoustic_token, '{}/utt2acoustic_token.pt'.format(args.dir))
    logging.info('spend time {}'.format(time.time() - start_time))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir',
                        type=str)
    parser.add_argument('--config_path',
                        type=str)
    parser.add_argument('--ckpt_path',
                        type=str)
    parser.add_argument('--sample_rate',
                        default=24000,
                        type=int)
    # parser.add_argument('--onnx_path',
    #                     type=str)
    args = parser.parse_args()
    
    main(args)