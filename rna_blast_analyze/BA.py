#!/usr/bin/env python3
import argparse
import sys
import json
import os

import logging
# this must precede the CONFIG
logger = logging.getLogger('rna_blast_analyze')

ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)

logger.addHandler(ch)


from rna_blast_analyze.BR_core import BA_verify
from rna_blast_analyze.BR_core import cmalign
from rna_blast_analyze.BR_core.expand_by_LOCARNA import locarna_anchored_wrapper_inner
from rna_blast_analyze.BR_core.expand_by_BLAST import blast_wrapper_inner
from rna_blast_analyze.BR_core.expand_by_joined_pred_with_rsearch import joined_wrapper_inner
from rna_blast_analyze.BR_core.config import tools_paths, CONFIG


class ParseFilter(argparse.Action):
    def __call__(self, parser, args, values, option_string=None):
        ops1 = {'>', '<', '='}
        ops2 = {'>=', '<='}
        if values[:2] in ops2:
            setattr(args, self.dest, (values[:2], float(values[2:])))
        elif values[0] in ops1:
            setattr(args, self.dest, (values[0], float(values[1:])))
        else:
            print('Do not understand operator. Allowed are: >, <, =, <= and >=.')
            raise argparse.ArgumentError


def f_parser():
    """
    Input parser
    :return: args structure
    """
    parser = argparse.ArgumentParser(
        description='Blast RNA Refine pipeline',
    )
    input_group = parser.add_argument_group(
        'INPUT'
    )
    output_group = parser.add_argument_group(
        'OUTPUT'
    )
    parameters_group = parser.add_argument_group(
        'PARAMETERS'
    )
    misc_group = parser.add_argument_group(
        'MISC'
    )
    input_group.add_argument(
        '-blast_in',
        type=str,
        required=True,
        metavar='PATH',
        help='BLAST output file with hits to analyze.'
    )
    input_group.add_argument(
        '-blast_query',
        default=None,
        required=True,
        type=str,
        metavar='PATH',
        help='The Blast query fasta file.'
    )
    input_group.add_argument(
        '-blast_db',
        required=True,
        metavar='path',
        type=str,
        help='Provide path to blast database, '
             'that is the complete path with na name without any extensions '
             '(*.nin, nsd, nog, nsi, nhr, nsq, nal).'
    )
    misc_group.add_argument(
        '--b_type',
        default='guess',
        choices=['guess', 'xml', 'plain']
    )
    misc_group.add_argument(
        '--blast_regexp',
        type=str,
        # default='(?<=\|)[A-Z0-9]*\.?\d*$',
        default='[A-Z0-9a-z_]+\.[1-9]+',
        help='Provide python valid regular expression which capture the index key to blastdb'
        ' (usualy the accession.version number).'
    )
    parameters_group.add_argument(
        '--mode',
        type=str,
        default='locarna',
        choices=['simple', 'locarna', 'joined'],
        help='Choose mode of hit elongation: '
             'simple (extend by unaligned parts of query) '
             'locarna (run locarna algorithm - uses secondary structure for better alignment) '
             'joined (uses both methods and chooses the alignment which has better RSEARCH score).'
    )
    parameters_group.add_argument(
        '--subseq_window',
        type=int,
        default=10,
        help='N of nucleotides of which can subsequence differ in length,'
        ' also the maximum expansion of sequence ahead and behind the query seq.'
    )
    output_group.add_argument(
        '--html',
        metavar='PATH',
        type=str,
        help='Output html file with secondary structure pictures and other useful stuff.'
    )
    misc_group.add_argument(
        '--threads',
        default=None,
        type=int,
        metavar='N',
        help='Number of threads to use (default = N of logical cores detected).'
    )
    output_group.add_argument(
        '--csv',
        default=None,
        help='Output in csv table, infered sequence and structure present.'
    )
    output_group.add_argument(
        '--json',
        type=str,
        metavar='PATH',
        default=None,
        help='Dump all stored data to JSON (developer only - it is possible to convert to all other output formats).'
    )
    misc_group.add_argument(
        '--cm_file',
        default=None,
        type=str,
        metavar='CM_file',
        help='Provided covariance model will be used for homology inference instead of RSEARCH model.'
    )
    misc_group.add_argument(
        '--use_rfam',
        action='store_true',
        default=False,
        help='Search in rfam database for covariance model to infer homology with instead of RSEARCH model.'
    )
    misc_group.add_argument(
        '--download_rfam',
        action='store_true',
        default=False,
        help='Retrieve RFAM covariance models database. Will download only if new version avalible.'
    )
    misc_group.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.0.1'
    )
    parameters_group.add_argument(
        '--config_file',
        type=str,
        default=None,
        metavar='PATH',
        help='Provide config file if tools and data are in non-default paths.'
    )
    parameters_group.add_argument(
        '-pm',
        '--prediction_method',
        nargs='*',
        type=str,
        metavar='prediction_method_name',
        default=['pairwise_centroid_homfold', 'rfam_rnafoldc', 'rnafold'],
        choices=BA_verify.pred_method_required_tools.keys(),
        help='Prediction method to use. Multiple are allowed.'
    )
    parameters_group.add_argument(
        '--pm_param_file',
        type=str,
        metavar='PATH',
        default=None,
        help='File with parameters for prediction methods in JSON. Nondeclared prediction methods treated as defaults.'
    )
    misc_group.add_argument(
        '--logfile',
        type=str,
        default=None,
        metavar='logfile'
    )
    # todo add help
    parameters_group.add_argument(
        '--subseq_window_simple_ext',
        type=int,
        default=10,
        help=argparse.SUPPRESS
    )
    parameters_group.add_argument(
        '--subseq_window_locarna',
        type=int,
        default=30,
        help='N of nucleotides to add before realignment.'
    )
    parameters_group.add_argument(
        '--locarna_params',
        type=str,
        default='--struct-local=0 --sequ-local=0 --free-endgaps=++++',
    )
    parameters_group.add_argument(
        '--locarna_anchor_length',
        type=int,
        default=7,
        help='Minimal number of adjacent matching bases in BLAST hit to create an anchor for Locarna.'
    )
    parser.add_argument(
        '--repredict_file',
        type=str,
        metavar='PATH',
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--dev_pred',
        action='store_true',
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--dump',
        type=str,
        metavar='PATH',
        default=None,
        help=argparse.SUPPRESS,
        # help='if given, result data will be dump in python pickle, the datastructure can change',
    )
    parser.add_argument(
        '--pandas_dump',
        type=str,
        metavar='PATH',
        default=None,
        help=argparse.SUPPRESS,
        # help='same data as with --csv but in binary (pandas pickle) format'
    )
    parser.add_argument(
        '--keep_all',
        default=False,
        type=bool,
        # help='keep all files (debug)'
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--show_gene_browser',
        default=True,
        type=bool,
        # help='option to hide gene browser for debugging output web page'
        help=argparse.SUPPRESS,
    )
    mu = misc_group.add_mutually_exclusive_group()
    mu.add_argument(
        '--filter_by_eval',
        default=None,
        action=ParseFilter,
        help='Filter the input blast by E-value. Only hits following the rule will be kept. '
             'Example ">10e-10" will keep only hits with eval greater then 10e-10.'
    )
    mu.add_argument(
        '--filter_by_bitscore',
        default=None,
        action=ParseFilter,
        help='Filter the input blast by bit score. Only hits following the rule will be kept. '
             'Example "<20" will keep only hits with bit score less then 20.'
    )
    misc_group.add_argument(
        '-v', '--verbose',
        dest='verbose',
        action='count',
        default=0,
        help='output verbosity -> most detailed -vv (lot of output)'
    )
    args = parser.parse_args()
    args.command = sys.argv

    # handle prediction params input file
    # fallback to default
    default_param_file = os.path.abspath(
            os.path.dirname(__file__) + os.sep + os.path.join('BR_core', 'prediction_parameters.json')
        )
    params = dict()
    with open(default_param_file, 'r') as ff:
        default_params = json.load(ff)
        params.update(default_params)

    if args.pm_param_file:
        with open(args.pm_param_file, 'r') as ff:
            provided_params = json.load(ff)
            params.update(provided_params)

    # set logger level
    logger.setLevel(max(3 - args.verbose, 1) * 10)

    # check if prediction method params are valid
    BA_verify.check_params(params)

    args.pred_params = params
    return args


def check_if_rfam_needed(inargs):
    """
    check if we need to have rfam defined
    :param inargs:
    :return:
    """
    if inargs.use_rfam:
        return True
    elif any([True for pmname in inargs.prediction_method if 'rfam' in pmname]):
        return True
    else:
        return False


def main():

    # outer envelope for the script
    # ========= perform argument parsing =========
    args = f_parser()
    logger.debug('parsed arguments: {}'.format(args))

    # create logging file if requested
    if args.logfile:
        fh = logging.FileHandler(args.logfile)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    logger.info('RNA BLAST analyze pipeline start.')

    logger.info('BLAST file: {}'.format(args.blast_in))
    logger.info('Query file: {}'.format(args.blast_query))
    logger.info('BLAST db:   {}'.format(args.blast_db))
    if args.config_file:
        logger.info('configfile: {}'.format(args.config_file))

    # ========= load optional cfg file =========
    CONFIG.override(tools_paths(config_file=args.config_file))

    # ========= check if tools needed for requested methods are installed =========
    BA_verify.check_necessery_tools(methods=args.prediction_method)

    # ========= check rfam =========
    if check_if_rfam_needed(args):
        if not args.download_rfam and not cmalign.check_rfam_present():
            raise ValueError(
                'RFAM models file is not present in specified path. '
                'Please enable rfam download or provide prepared directory.'
            )

    if args.download_rfam:
        cmalign.download_cmmodels_file()

    # ========= run =========
    if args.mode == 'simple':
        blast_wrapper_inner(args)
    elif args.mode == 'locarna':
        locarna_anchored_wrapper_inner(args)
    elif args.mode == 'joined':
        joined_wrapper_inner(args)
    else:
        raise ValueError('Unknown option - should be cached by argparse.')


if __name__ == '__main__':
    main()
