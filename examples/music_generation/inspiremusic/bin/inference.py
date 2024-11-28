# Copyright (c) 2024 Alibaba Inc
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

from __future__ import print_function

import argparse
import logging
logging.getLogger('matplotlib').setLevel(logging.WARNING)
import os
import torch
from torch.utils.data import DataLoader
import torchaudio
from hyperpyyaml import load_hyperpyyaml
from tqdm import tqdm
from inspiremusic.cli.model import InspireMusicModel
from inspiremusic.dataset.dataset import Dataset
import random
import time
from inspiremusic.utils.audio_utils import trim_audio

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_args():
    parser = argparse.ArgumentParser(description='inference only with your model')
    parser.add_argument('--config', required=True, help='config file')
    parser.add_argument('--prompt_data', required=True, help='prompt data file')
    parser.add_argument('--flow_model', required=True, help='flow model file')
    parser.add_argument('--llm_model', default=None,required=False, help='flow model file')
    parser.add_argument('--music_tokenizer', required=True, help='music tokenizer model file')
    parser.add_argument('--wavtokenizer', required=True, help='wavtokenizer model file')
    parser.add_argument('--chorus', default="verse",required=False, help='chorus tag generation mode, eg. random, verse, chorus, intro.')
    parser.add_argument('--no_flow', default=False, required=False, help='inference with out flow matching model')
    parser.add_argument('--fp16', default=True, required=False, help='inference with out flow matching model')
    parser.add_argument('--trim', default=True, required=False, help='trim generated audio')
    parser.add_argument('--sample_rate', type=int, default=24000, required=False,
                        help='sampling rate of generated audio')
    parser.add_argument('--min_generate_audio_seconds', type=float, default=8.0, required=False,
                        help='the minimum generated audio length in seconds')
    parser.add_argument('--max_generate_audio_seconds', type=float, default=30.0, required=False,
                        help='the maximum generated audio length in seconds')
    parser.add_argument('--gpu',
                        type=int,
                        default=-1,
                        help='gpu id for this rank, -1 for cpu')
    parser.add_argument('--mode',
                        default='sft',
                        choices=['sft', 'zero_shot'],
                        help='inference mode')
    parser.add_argument('--result_dir', required=True, help='asr result file')
    # parser.add_argument('--caras_sampling', default=False, required=False, help='use ras sampling')
    args = parser.parse_args()
    print(args)
    return args


def main():
    music_forms = ["intro", "verse1", "chorus", "verse2", "outro"]

    args = get_args()
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(levelname)s %(message)s')
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)

    min_generate_audio_length = int(args.sample_rate * args.min_generate_audio_seconds)
    max_generate_audio_length = int(args.sample_rate * args.max_generate_audio_seconds)
    assert args.min_generate_audio_seconds <= args.max_generate_audio_seconds

    # Init inspiremusic models from configs
    use_cuda = args.gpu >= 0 and torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    with open(args.config, 'r') as f:
        configs = load_hyperpyyaml(f)

    model = InspireMusicModel(configs['llm'], configs['flow'], configs['hift'], configs['wavtokenizer'], args.no_flow, args.fp16)
    model.load(args.llm_model, args.flow_model, args.music_tokenizer, args.wavtokenizer)
    if args.llm_model is None:
        model.llm = None
    else:
        model.llm = model.llm.to(torch.float32)

    if args.flow_model is None:
        model.flow = None

    test_dataset = Dataset(args.prompt_data, data_pipeline=configs['data_pipeline'], mode='inference', shuffle=True, partition=False)
    test_data_loader = DataLoader(test_dataset, batch_size=None, num_workers=0)

    del configs
    os.makedirs(args.result_dir, exist_ok=True)
    fn = os.path.join(args.result_dir, 'wav.scp')
    f = open(fn, 'w')
    caption_fn = os.path.join(args.result_dir, 'captions.txt')
    caption_f = open(caption_fn, 'w')

    with torch.no_grad():
        for _, batch in tqdm(enumerate(test_data_loader)):
            utts = batch["utts"]

            assert len(utts) == 1, "inference mode only support batchsize 1"
            text_token = batch["text_token"].to(device)
            text_token_len = batch["text_token_len"].to(device)

            if "time_start" not in batch.keys():
                batch["time_start"] = torch.randint(0, args.min_generate_audio_seconds, (1,)).to(torch.float64)
            if "time_end" not in batch.keys():
                batch["time_end"] = torch.randint(args.min_generate_audio_seconds, args.max_generate_audio_seconds, (1,)).to(torch.float64)
            elif (batch["time_end"].numpy()[0] - batch["time_start"].numpy()[0]) < args.min_generate_audio_seconds:
                batch["time_end"] = torch.randint(int(batch["time_start"].numpy()[0] + args.min_generate_audio_seconds), int(batch["time_start"].numpy()[0] + args.max_generate_audio_seconds), (1,)).to(torch.float64)
                # batch["time_end"] =  torch.Tensor([int(batch["time_start"].numpy()[0] + args.max_generate_audio_seconds)]).to(torch.float64)
            
            # batch["time_start"] = torch.Tensor([60.0]).to(torch.float64)
            # batch["time_end"] = torch.Tensor([70.0]).to(torch.float64)

            if "chorus" not in batch.keys():
                batch["chorus"] = torch.randint(1, 5, (1,))
            
            if args.chorus == "random":
                batch["chorus"] = torch.randint(1, 5, (1,))
            elif args.chorus == "intro":
                batch["chorus"] = torch.Tensor([0])
            elif "verse" in args.chorus:
                batch["chorus"] = torch.Tensor([1])
            elif args.chorus == "chorus":
                batch["chorus"] = torch.Tensor([2])
            elif args.chorus == "outro":
                batch["chorus"] = torch.Tensor([4])

            time_start = batch["time_start"].to(device)
            time_end = batch["time_end"].to(device)
            chorus = batch["chorus"].to(torch.int)

            text_prompt = f"<|{batch['time_start'].numpy()[0]}|><|{music_forms[chorus.numpy()[0]]}|><|{batch['text'][0]}|><|{batch['time_end'].numpy()[0]}|>"
            chorus = chorus.to(device)
            audio_token = batch["acoustic_token"].to(device)
            audio_token_len = batch["acoustic_token_len"].to(device)
            text = batch["text"]

            if "semantic_token" in batch:                      
                token  = batch["semantic_token"].to(device)  
                token_len  = batch["semantic_token_len"].to(device)   
            else:                                                               
                token = audio_token.view(audio_token.size(0),-1,4)[:,:,0]
                token_len  = audio_token_len / 4   

            if args.mode == 'sft':
                model_input = {"text": text_token, "audio_token": token, "audio_token_len": token_len,
                                "text_token": text_token, "text_token_len": text_token_len,
                                "embeddings": [time_start, time_end, chorus], "raw_text":text}
            else:
                model_input = {'text': text_token, 'text_len': text_token_len,
                            'prompt_text': text_token, 'prompt_text_len': text_token_len,
                            'llm_prompt_audio_token': audio_token, 'llm_prompt_audio_token_len': audio_token_len,
                            'flow_prompt_audio_token': audio_token, 'flow_prompt_audio_token_len': audio_token_len,
                            'prompt_audio_feat': audio_feat, 'prompt_audio_feat_len': audio_feat_len,
                            'llm_embedding': utt_embedding, 'flow_embedding': utt_embedding}

            music_key = utts[0]
            music_audios = []
            music_fn = os.path.join(args.result_dir, '{}.wav'.format(music_key))
            bench_start = time.time()
            for model_output in model.inference(**model_input):
                music_audios.append(model_output['music_audio'])
            bench_end = time.time()
            if args.trim:
                music_audio = trim_audio(music_audios[0], sample_rate=args.sample_rate, threshold=0.05, min_silence_duration=0.8)
            else:
                music_audio = music_audios[0]
            if music_audio.shape[0] != 0:
                if music_audio.shape[1] > max_generate_audio_length:
                    music_audio = music_audio[:,:max_generate_audio_length]
                if music_audio.shape[1] >= min_generate_audio_length:
                    try:
                        torchaudio.save(music_fn, music_audio, sample_rate=args.sample_rate, bits_per_sample=16)
                    except Exception as e:
                        logging.info(f"Error saving file: {e}")
                        raise
                    
                    audio_duration = music_audio.shape[1] / args.sample_rate
                    rtf = (bench_end - bench_start) / audio_duration
                    logging.info(f"processing time: {int(bench_end - bench_start)}s, audio length: {int(audio_duration)}s, rtf: {rtf}, text prompt: {text_prompt}")
                    f.write('{} {}\n'.format(music_key, music_fn))
                    f.flush()
                    caption_f.write('{}\t{}\n'.format(music_key, text_prompt))
                    caption_f.flush()
                else:
                    logging.info(f"Generate audio length {music_audio.shape[1]} is shorter than min_generate_audio_length.")
            else:
                logging.info(f"Generate audio is empty, dim = {music_audio.shape[0]}.")
    f.close()
    logging.info('Result wav.scp saved in {}'.format(fn))

if __name__ == '__main__':
    main()