#!/usr/bin/env python3

import argparse
import hashlib
import logging
import os
import sys
from typing import List, Dict, Iterator, Any, Tuple

import numpy as np
import sentencepiece as spm
import torch
from fairseq import checkpoint_utils
from fairseq.data import LanguagePairDataset
from sacrebleu import get_source_file, get_reference_files, DATASETS, get_langpairs_for_testset

logger = logging.getLogger('prism')
logger.setLevel(logging.INFO)

MODELS = {
    '8412b2044da4b9b2c0a8ce87b305d0d1': {
        'name': 'm39v1',
        'path': 'todo',
        'date': '2020-04-30',
        'description': 'model released with arXiv paper April 2020',
        'langs': ['ar', 'bg', 'bn', 'ca', 'cs', 'da', 'de', 'el', 'en', 'es', 'et', 'eo', 'fi', 'fr', 'he',
                  'hr', 'hu', 'id', 'it', 'ja', 'kk', 'lt', 'lv', 'mk', 'nl', 'no', 'pl', 'pt', 'ro', 'ru',
                  'sk', 'sl', 'sq', 'sr', 'sv', 'tr', 'uk', 'vi', 'zh'],
    }
}


def hash_model(model_dir):
    md5 = hashlib.md5()
    block_size = 2 ** 20
    for fname in ('checkpoint.pt', 'spm.model', 'dict.src.txt', 'dict.tgt.txt'):
        with open(os.path.join(model_dir, fname), "rb") as f:
            while True:
                data = f.read(block_size)
                if not data:
                    break
                md5.update(data)
    md5.digest()
    return md5.hexdigest()


class Prism:
    def __init__(self, model_dir, lang):
        '''
        model_dir should contain:
         1) checkpoint.pt: the fairseq model
         2) spm.model: the sentencepiece model
         3) dict.src.txt: the fairseq source dictionary
         4) dict.tgt.txt: the fairseq target dictionary (likely a copy of the source)

        lang: ISO 639-1 Code (e.g. "en"). Must be a language compatable with the model.
        '''
        self.sp = spm.SentencePieceProcessor()
        self.sp.Load(model_dir + '/spm.model')

        self.lang = lang

        # this prints things and I can't figure out how to disable it
        sys.stdout = open(os.devnull, 'w')
        self.models, self.args, self.task = checkpoint_utils.load_model_ensemble_and_task(
            [model_dir + '/checkpoint.pt', ],
            arg_overrides=dict(data=model_dir + '/'),
        )
        sys.stdout = sys.__stdout__

        self.use_cuda = torch.cuda.is_available()

        self.args.score_reference = True
        self.args.print_alignment = False  # this is ignored, appears to be fairseq bug, makes things WAY slower
        self.generator = self.task.build_generator(self.args)

        for model in self.models:
            if self.use_cuda:
                model.cuda()
            model.make_generation_fast_(
                beamable_mm_beam_size=None,
                need_attn=False,
            )
            # if model.args.fp16:
            #    model.half()

        # hash model
        self.model_hash = hash_model(model_dir)

        if self.model_hash in MODELS:
            model_langs = MODELS[self.model_hash]['langs']
            if lang not in model_langs:
                model_name = MODELS[self.model_hash]['name']
                logger.warning(f'Language "{lang}" is unsupported for model "{model_name}"')
                logger.warning(f'Supported languages for {model_name}: {", ".join(model_langs)}')
                sys.exit(1)
        else:
            logger.warning('unrecognized model, so cannot check language')

    def identifier(self):
        if self.model_hash in MODELS:
            model_name = MODELS[self.model_hash]['name']
        else:
            logger.warning('unrecognized model, using hash to identify')
            model_name = self.model_hash

        return dict(version='0.1', model=model_name, seg_scores='avg_log_prob', sys_scores='avg_log_prob', log_base=2)

    def _binarize(self, sentence: str) -> torch.LongTensor:
        return self.task.source_dictionary.encode_line(sentence, add_if_not_exist=False).long()

    def _encode(self, sent, prepend=True):
        sent = ' '.join(self.sp.EncodeAsPieces(sent))
        if prepend:
            sent = f'<{self.lang}> ' + sent
        return self._binarize(sent)

    def _build_batches(self,
                       source_tokens: List[List[int]],
                       target_tokens: List[List[int]],
                       skip_invalid_size_inputs: bool) -> Iterator[Dict[str, Any]]:
        source_lengths = torch.LongTensor([t.numel() for t in source_tokens])
        target_lengths = torch.LongTensor([t.numel() for t in target_tokens])

        batch_iterator = self.task.get_batch_iterator(
            dataset=LanguagePairDataset(source_tokens, source_lengths, self.task.source_dictionary,
                                        tgt=target_tokens, tgt_sizes=target_lengths,
                                        tgt_dict=self.task.target_dictionary),
            max_tokens=self.args.max_tokens,
            max_sentences=self.args.max_sentences,
            max_positions=(2000, 2000),  # ???
            ignore_invalid_inputs=skip_invalid_size_inputs,
        ).next_epoch_itr(shuffle=False)
        return batch_iterator

    def _score_forward(self, tok_sents_in, tok_sents_out):
        assert len(tok_sents_in) == len(tok_sents_out)
        tok_level_scores = [None, ] * len(tok_sents_in)  # for debug
        results = [None, ] * len(tok_sents_in)
        for batch in self._build_batches(tok_sents_in, tok_sents_out, skip_invalid_size_inputs=False):
            if self.use_cuda:  # must be a better way
                batch['id'] = batch['id'].cuda()
                batch['net_input']['src_tokens'] = batch['net_input']['src_tokens'].cuda()
                batch['net_input']['src_lengths'] = batch['net_input']['src_lengths'].cuda()
                batch['net_input']['prev_output_tokens'] = batch['net_input']['prev_output_tokens'].cuda()
                batch['target'] = batch['target'].cuda()

            translations = self.task.inference_step(self.generator, self.models, batch)

            ids = batch['id'].cpu().numpy()

            tok_scores = [x[0]['positional_scores'].cpu().numpy() for x in translations]

            # [1:] to skip language tag log prob
            sent_scores = [np.mean(x[1:]) for x in tok_scores]

            for _id, sent_score, _tok_score in zip(ids, sent_scores, tok_scores):
                results[_id] = sent_score
                tok_level_scores[_id] = _tok_score

        if logger.level == logging.DEBUG:
            for ii, (sent_in, scores_out, sent_out) in enumerate(zip(tok_sents_in, tok_level_scores, tok_sents_out)):
                sent_in_str = ' '.join([self.task.source_dictionary[x] for x in sent_in])
                logger.debug(f'Input[{ii}] = ' + sent_in_str)
                sent_out_tok = [self.task.source_dictionary[x] for x in sent_out]
                logger.debug(f'Output[{ii}] = ' + \
                             f' '.join([f'{a}[{b:.02f}]' for a, b in zip(sent_out_tok, scores_out)]))

        if None in results:
            raise Exception('Missing one or more sentence scores')

        return np.array(results)

    def score(self, cand, ref=None, src=None, segment_scores=False):

        if not (ref is None) ^ (src is None):
            raise Exception('Must provide exactly one of "ref" or "src"')

        tokenized_cand = [self._encode(sentence, prepend=False) for sentence in cand]
        tokenized_cand_prep = [self._encode(sentence, prepend=True) for sentence in cand]

        if src is not None:
            # Prism-src: score candidate given on source
            if len(cand) != len(src):
                raise Exception(f'Length of cand ({len(cand)}) does not match length of src ({len(src)})')
            tokenized_src = [self._encode(sentence, prepend=False) for sentence in src]
            scores = self._score_forward(tokenized_src, tokenized_cand_prep)

        else:
            # Prism-ref: average candidate given reference and reference given candidate
            if len(cand) != len(ref):
                raise Exception(f'Length of cand ({len(cand)}) does not match length of ref ({len(ref)})')
            tokenized_ref = [self._encode(sentence, prepend=False) for sentence in ref]
            tokenized_ref_prep = [self._encode(sentence, prepend=True) for sentence in ref]
            forward_scores = self._score_forward(tok_sents_in=tokenized_ref, tok_sents_out=tokenized_cand_prep)
            reverse_scores = self._score_forward(tok_sents_in=tokenized_cand, tok_sents_out=tokenized_ref_prep)
            scores = 0.5 * forward_scores + 0.5 * reverse_scores

        if not segment_scores:
            scores = np.mean(scores)

        return scores


def parse_sacrebleu_uri(uri: str) -> Tuple[str]:
    """
    Parses the test set and language pair from a URI of the form

        sacrebleu:wmt19:de-en
        sacrebleu:wmt19/google/ar:de-en
    """
    try:
        _, testset, langpair = uri.split(":")
    except ValueError:
        logger.error('sacrebleu:* flags must take the form "sacrebleu:testset:langpair"')
        sys.exit(1)

    testsets = sorted(DATASETS, reverse=True)
    if testset not in testsets:
        logger.error(f"Test set '{testset}' was not found. Available sacrebleu test sets are:")
        for key in testsets:
            logger.error(f"  {key:20s}: {DATASETS[key].get('description', '')}")
        sys.exit(1)

    lang_pairs = get_langpairs_for_testset(testset)

    if langpair not in lang_pairs:
        logger.error(f"Language pair '{langpair}' not available for testset '{testset}'.\n"
                     f" Language pairs available for {testset}: {', '.join(lang_pairs)}")
        sys.exit(1)

    return testset, langpair


def main():
    parser = argparse.ArgumentParser(description='Prism: MT metric based on multilingual NMT')
    parser.add_argument('--cand', required=False, type=argparse.FileType('rt'), default=sys.stdin,
                        help='Candidate text file. If not provided, candidates are read from stdin.')
    parser.add_argument('--ref', required=False, type=str,
                        help='Reference text file. If provided, reference-based Prism-ref scores are returned. '
                             'A value of "sacrebleu:{testset}:{langpair}" will use sacrebleu datasets. '
                             'You must provide exactly one of --ref or --src. ')
    parser.add_argument('--src', required=False, type=str,
                        help='Source text file. If provided, source-based Prism-src scores are returned. '
                             'A value of "sacrebleu:{testset}:{langpair}" will use sacrebleu datasets. '
                             'You must provide exactly one of --ref or --src.')
    parser.add_argument('--model-dir', required=True, type=str, help='Model Directory')
    parser.add_argument('--lang', type=str, help='2-character language code (ISO 639-1)')
    parser.add_argument('--segment-scores', action='store_true',
                        help='Print per-sentence scores instead of corpus level score')
    parser.add_argument('--debug', action='store_true', help='Print debug info')

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    if not (args.ref is None) ^ (args.src is None):
        logger.error('You must provide exactly one of --ref or --src')
        sys.exit(1)

    if args.ref is not None:
        if args.ref.startswith('sacrebleu:'):
            testset, langpair = parse_sacrebleu_uri(args.ref)
            path = get_reference_files(testset, langpair)[0]
            args.ref = open(path).readlines()
            args.lang = langpair.split("-")[1]
            logger.info(f"Scoring against {len(args.ref)}-line {args.lang} reference"
                        f" from sacrebleu dataset {testset}/{langpair}")
        else:
            args.ref = open(args.ref, 'rt').readlines()

    if args.src is not None:
        if args.src.startswith('sacrebleu:'):
            testset, langpair = parse_sacrebleu_uri(args.src)
            path = get_source_file(testset, langpair)
            args.src = open(path).readlines()
            args.lang = langpair.split("-")[0]
            logger.info(f"Scoring against {len(args.src)}-line {args.lang} source"
                        f" from sacrebleu dataset {testset}/{langpair}")
        else:
            args.src = open(args.src, 'rt').readlines()

    if args.lang is None:
        logger.error("The language must be specified (--lang XX), XX the ISO 639-1 code")
        sys.exit(1)

    args.cand = args.cand.readlines()

    n_gpus = torch.cuda.device_count()
    logging.debug(f'Running on {"GPU" if n_gpus else "CPU"}')
    if len(args.cand) > 50 and n_gpus == 0:
        logging.warning('Running on CPU is slow...')

    prism = Prism(model_dir=args.model_dir, lang=args.lang)
    scores = prism.score(cand=args.cand, ref=args.ref, src=args.src, segment_scores=args.segment_scores)

    logger.info(f'Prism identifier: {prism.identifier()}')

    if args.segment_scores:
        for ss in scores:
            print(ss)
    else:
        print(scores)


if __name__ == '__main__':
    main()
