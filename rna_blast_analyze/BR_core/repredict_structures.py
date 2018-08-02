import os
import pickle
import re
import sys
from tempfile import mkstemp
from warnings import warn

import dill
import numpy as np
import pandas

import rna_blast_analyze.BR_core.BA_methods
from rna_blast_analyze.BR_core.cmalign import RfamInfo, run_cmfetch, get_cm_model
from rna_blast_analyze.BR_core.infer_homology import _infer_hits_cm
from rna_blast_analyze.BR_core.predict_structures import alifold_refold_prediction, tcoffee_rcoffee_refold_prediction, \
    decouple_homologs_alifold_refold_prediction, rnafold_prediction, subopt_fold_query,\
    subopt_fold_alifold, msa_alifold_rapidshapes, cmscan_rapidshapes, cmmodel_rnafold_c,\
    rfam_subopt_pred, turbofold_conservative_prediction, turbofold_only_homologous
from rna_blast_analyze.BR_core.centroid_homfold import me_centroid_homfold
from rna_blast_analyze.BR_core.predict_structures import find_nc_and_remove, check_lonely_bp, IUPACmapping
from Bio import AlignIO, SeqIO
from Bio.Phylo.TreeConstruction import DistanceCalculator
from Bio.SeqRecord import SeqRecord

import rna_blast_analyze.BR_core.BA_support as BA_support
from rna_blast_analyze.BR_core.BA_support import NoHomologousSequenceException

safe_prediction_method = [
    'rnafold',
    'pairwise_centroid_homfold',
    'TurboFold_conservative',
]


def filter_ambiguous_seqs_from_list(seqlist):
    return [seq for seq in seqlist if not seq.annotations['ambiguous']]


def select_homologous_sequences(all_hits, cmscore=None, selection_threshold=0, cm_percent_threshold=None):
    # if cm_percent_threshold is given, selection threshold must not be given
    assert (selection_threshold == 0) or cm_percent_threshold is None
    if cmscore is None:
        score = _extract_cmscore_from_hom_seqs(all_hits)
    else:
        assert len(cmscore) == len(all_hits)
        score = cmscore

    if cm_percent_threshold is not None:
        mcm = max(score)
        selection_threshold = cm_percent_threshold*mcm/100

    pred = _infer_hits_cm(score, tr=selection_threshold)
    return [i for i, j in zip(all_hits, pred) if j]


def wrapped_ending_with_prediction(args_inner, analyzed_hits, all_hits_fasta, query,
                                   pred_method=None, method_params=None):
    """
    wrapper for prediction of secondary structures
    :param args_inner: Namespace of input arguments
    :param analyzed_hits: BlastSearchRecompute object
    :param all_hits_fasta: fasta file with all extended sequences
    :param query: query sequence
    :param pred_method:
    :param method_params:
    :return:
    """
    if pred_method is None:
        pred_method = args_inner.prediction_method

    # todo: remove later - quick fix because some test code assigns prediction parameter for one method as str
    if isinstance(pred_method, str):
        pred_method = (pred_method,)

    if method_params is None:
        method_params = args_inner.pred_params

    new_structures, exe_time = repredict_structures_for_homol_seqs(
        analyzed_hits.query,
        all_hits_fasta,
        args_inner.threads,
        prediction_method=pred_method,
        pred_method_params=method_params,
        all_hits=analyzed_hits.hits,
        )

    if 'default' not in pred_method:
        for i, hit in enumerate(analyzed_hits.hits):
            for key in new_structures.keys():
                assert str(hit.subs[hit.ret_keys[0]].seq) == str(new_structures[key][i].seq)
                hit.subs[hit.ret_keys[0]].annotations['sss'] += [key]

                # expects "predicted" in annotations - for now, if not given, default is True, as not all prediction
                #  methods implement "predicted" in their output
                if new_structures[key][i].annotations.get('predicted', True):
                    hit.subs[hit.ret_keys[0]].letter_annotations[key] = new_structures[key][i].letter_annotations['ss0']

    else:
        # default in pred method
        pass

    if 'default' not in pred_method:
        for hit in analyzed_hits.hits:
            del hit.subs[hit.ret_keys[0]].letter_annotations['ss0']
            hit.subs[hit.ret_keys[0]].annotations['sss'].remove('ss0')
    else:
        for i, hit in enumerate(analyzed_hits.hits):
            # assert str(hit.subs[hit.ret_keys[0]].seq) == str(new_structures[key][i].seq)
            hit.subs[hit.ret_keys[0]].annotations['sss'].remove('ss0')
            hit.subs[hit.ret_keys[0]].annotations['sss'] += ['default']
            hit.subs[hit.ret_keys[0]].letter_annotations['default'] = hit.subs[hit.ret_keys[0]].letter_annotations.pop('ss0')

    # do not need this, for the sake of compatibility with evaluation scripts
    template_structure = BA_support.RNAfold(str(query.seq))[1]
    # templates = {'t0': {'str': RNA.fold(str(query.seq))[0],
    #                     'seq': str(query.seq)}}
    templates = {'t0': {'str': template_structure,
                        'seq': str(query.seq)}}

    for hit in analyzed_hits.hits:
        hit.templates = templates

    # remove uid from file descriptor
    analyzed_hits = rna_blast_analyze.BR_core.BA_methods.remove_uid_internal_descriptor(analyzed_hits)

    # write html if requested
    if args_inner.html:
        analyzed_hits.to_html(args_inner.html)

    # write csv file if requested
    if args_inner.csv:
        analyzed_hits.to_csv(args_inner.csv)

    # replace with json
    if args_inner.json:
        analyzed_hits.to_json(args_inner.json, getattr(args_inner, 'zip_json', False))

    if args_inner.pandas_dump:
        pandas.to_pickle(analyzed_hits.pandas, args_inner.pandas_dump)

    if args_inner.dump:
        with open(args_inner.dump, 'wb') as pp:
            pickle.dump(analyzed_hits, pp, pickle.HIGHEST_PROTOCOL)

        with open(args_inner.dump[:-4] + 'time_dump', 'wb') as pp:
            pickle.dump(exe_time, pp, pickle.HIGHEST_PROTOCOL)

        with open(args_inner.dump[:-4] + 'dill', 'wb') as pp:
            dill.dump(analyzed_hits, pp, pickle.HIGHEST_PROTOCOL)


def create_nr_homolog_hits_file_MSA_safe(
        sim_threshold_percent=None,
        all_hits=None,
        query=None,
        cmscore_tr=-2.03,
        cm_threshold_percent=None,
        check_unambiguous=False,
        len_diff=0.1,
):
    """
    create non redundant homologous hits file

    multiple at minimum (2) sequences are needed for profile alignment for some alignmers
    so this function always return two or more sequences or raises exception

    :param sim_threshold_percent:
    :return:
    """

    # i need to leave query, even if with umbiguos basepairs in
    # because it is used as an reference during distance computation and subsequence selection,
    # however i dont need to have all homologous seqs there
    if check_unambiguous:
        all_hits = filter_ambiguous_seqs_from_list(all_hits)

    dist_table, homologous_seqs = _hom_selection_wrapper(
        all_hits,
        query,
        cmscore_tr,
        cm_threshold_percent,
        len_diff_=len_diff
    )

    if dist_table.size == 0:
        raise NoHomologousSequenceException

    to_include = BA_support.select_sequences_from_similarity_rec(
        dist_table,
        sim_threshold_percent=sim_threshold_percent
    )
    nr_homolog_hits = [homologous_seqs[i] for i in to_include]

    # final checking of nr homologs
    # if sequence is filtered here, it is ambiguos basepair in query
    # removing it is fine if multiple homologous sequences are present
    # the problem will arise when only 1 homologous sequence will remain
    # if we added sequence in previous step, raise exception, else behave like in prev step
    # what if trusted hit is only one?
    if len(nr_homolog_hits) < 2 and not check_unambiguous:
        # warn('Only one sequence is unique under defined sim_threshold_percent (including query)')
        warn('Only one sequence is unique under defined sim_threshold_percent (including query)\n'
             'Adding the most disimilar homologous sequence to non redundant sequences list')
        # dis_hom_index = dist_table.index.get_loc(dist_table[0].idxmin())
        dis_hom_index = dist_table[:, 0].argmin()
        nr_homolog_hits.append(SeqRecord(homologous_seqs[dis_hom_index].seq, id='dummy_seq_01'))
        del dis_hom_index

    elif len(nr_homolog_hits) < 2 and check_unambiguous:
        if len(filter_ambiguous_seqs_from_list(nr_homolog_hits)) == 0:
            # this mean query contain ambiguos bases
            raise NoHomologousSequenceException
        else:
            warn('Only one sequence is unique under defined sim_threshold_percent (including query)\n'
                 'Adding the most disimilar homologous sequence to non redundant sequences list')
            # dis_hom_index = dist_table.index.get_loc(dist_table[0].idxmin())
            dis_hom_index = dist_table[:, 0].argmin()
            nr_homolog_hits.append(SeqRecord(homologous_seqs[dis_hom_index].seq, id='dummy_seq_01'))
            del dis_hom_index
        homologous_seqs = filter_ambiguous_seqs_from_list(homologous_seqs)

    elif len(nr_homolog_hits) >= 2 and not check_unambiguous:
        pass

    elif len(nr_homolog_hits) > 2 and check_unambiguous:
        nr_homolog_hits = filter_ambiguous_seqs_from_list(nr_homolog_hits)
        homologous_seqs = filter_ambiguous_seqs_from_list(homologous_seqs)

    elif len(nr_homolog_hits) == 2 and check_unambiguous:
        homologous_seqs = filter_ambiguous_seqs_from_list(homologous_seqs)
        if len(filter_ambiguous_seqs_from_list(nr_homolog_hits)) == 1:
            # this mean that query contains ambiguous base
            # need to remove query from nr_homologs
            # should i attempt to replace it with some different sequence?
            # no too complicated
            # rather raise an exception
            raise NoHomologousSequenceException

    else:
        raise Exception()

    fd_h, nr_homo_hits_file = mkstemp()
    with os.fdopen(fd_h, 'w') as f:
        BA_support.write_fasta_from_list_of_seqrecords(f, nr_homolog_hits)

    return nr_homo_hits_file, homologous_seqs


def create_nr_homolog_hits_file_MSA_unsafe(sim_threshold_percent=None, all_hits=None, query=None, cmscore_tr=0.0,
                                           cm_threshold_percent=None, len_diff=0.1):
    """
    create non redundant homologous hits file
    :param sim_threshold_percent:
    :return:
    """
    dist_table, homologous_seqs = _hom_selection_wrapper(
        all_hits,
        query,
        cmscore_tr,
        cm_threshold_percent,
        len_diff_=len_diff
    )
    if dist_table.size == 0:
        nr_homolog_hits = [query]
    else:
        # normal execution
        to_include = BA_support.select_sequences_from_similarity_rec(
            dist_table,
            sim_threshold_percent=sim_threshold_percent
        )
        nr_homolog_hits = [homologous_seqs[i] for i in to_include]

    fd_h, nr_homo_hits_file = mkstemp()
    with os.fdopen(fd_h, 'w') as f:
        BA_support.write_fasta_from_list_of_seqrecords(f, nr_homolog_hits)

    return nr_homo_hits_file, homologous_seqs


def _extract_cmscore_from_hom_seqs(hom_seqs):
    """
    will return list of cm_scores as obtained from homology inference
     usefull for selecting sequences more relevant
    :param hom_seqs:
    :return:
    """
    return [i.annotations['cmstat']['bit_sc'] for i in hom_seqs]


def _hom_selection_wrapper(all_hits_, query_, cmscore_tr_, cm_threshold_percent_, len_diff_ = 0.1):
    """
    runs basic non_redundant sequences calculation (ie exact sequence match)
    selects homologous sequences from all hits list by cmscore threshold or by query sequence

    behaviour:
        will return distance array with similarities in % including query sequence and list of homologous sequences
        including query sequence

        if no sequence is homologous
        it will return empty array for distance matrix and list with query sequence

    :param all_hits_:
    :param query_:
    :param cmscore_tr_:
    :param cm_threshold_percent_:
    :return:
    """
    hom_seqs_ = select_homologous_sequences(
        all_hits_,
        selection_threshold=cmscore_tr_,
        cm_percent_threshold=cm_threshold_percent_
    )
    if len(hom_seqs_) == 0:
        print('No sequences from BLAST output infered homologous for structure prediction')
        return np.empty(0), [query_]
    homologous_seqs_ = [query_] + hom_seqs_

    # make nr list of sequences -> faster alignment
    # better selection
    nr_homologous_seqs_ = BA_support.non_redundant_seqs(homologous_seqs_)

    # check if the homologous sequence is not exact match as query
    #  (ie taking non redundant set would be only one sequence)
    if len(nr_homologous_seqs_) == 1:
        print('All sequences infered homologous are exact same as query.')
        return np.empty(0), [query_]

    # select only sequences in some predifined length range to query
    # this is needed for longish ncRNAs
    #   tolerate 10 % length difference?
    ref_len = len(query_)
    nr_len_select_homologous = [seq for seq in nr_homologous_seqs_ if ref_len * (1 - len_diff_) < len(seq) < ref_len * (1 + len_diff_)]

    # sanitize seq names (muscle has issues with too long names)
    san_hom_seqs, san_dict = BA_support.sanitize_fasta_names_in_seqrec_list(nr_len_select_homologous)

    c_fd, homologous_sequence_file_ = mkstemp()
    with os.fdopen(c_fd, 'w') as f:
        BA_support.write_fasta_from_list_of_seqrecords(f, san_hom_seqs)

    # consider to not to do this but if lot of homologs are present,
    # it is better to do this so consensus predictor is not confused
    # also consider running some rnastructure avare algorithm
    # also consensus structure prediction is impossible in only one sequence avalible
    # but needed for alifold

    # distance computation is problem, when only 2 sequences here
    # compute sequence similarity in another way
    # ==== solution
    # run some fast alignment, (clustalo, muscle)
    # load the result alignment with Bio.AlignIO
    # compute distance matrix with Bio.Phylo.TreeConstruction.DistanceCalculator
    # only need to adapt to distances from the distance matrix
    # def _refil_UT(df):
    #     l = len(df)
    #     for i in range(l):
    #         for j in range(l):
    #             if isnan(df[i][j]):
    #                 df[i][j] = df[j][i]
    #     return df

    align_file = BA_support.run_muscle(homologous_sequence_file_, reorder=True)
    alig = AlignIO.read(align_file, format='clustal')
    distance_calc = DistanceCalculator(model='identity')
    dist_mat = distance_calc.get_distance(alig)
    # rebuild index from sanitized
    orig_index = [san_dict[i] for i in dist_mat.names]
    dist_mat_pd = pandas.DataFrame.from_records(dist_mat.matrix, index=orig_index)
    # dist_table = _refil_UT(dist_mat_pd) # maybe not need this
    dist_table_ = (1 - dist_mat_pd.as_matrix()) * 100

    os.remove(align_file)
    os.remove(homologous_sequence_file_)
    return dist_table_, homologous_seqs_


def nonhomseqwarn(method_name):
    msg = 'No sequence was infered homologous, need at least 1 for {} type of prediction\n' \
          'Try one of following prediction methods: {}.'.format(
              method_name,
              ', '.join(safe_prediction_method)
          )
    warn(msg, RuntimeWarning)
    sys.stderr.flush()


def annotate_ambiguos_bases(seqlist):
    iupac = IUPACmapping()
    reg = re.compile("[^" + "^".join(iupac.unambiguous) + "]+", re.IGNORECASE)
    for seq in seqlist:
        m = re.search(reg, str(seq.seq))
        if m:
            msg = "Ambiguous base dected in {}, violating base {}, pos {}".format(
                seq.id,
                m.group(),
                m.start()
            )
            warn(msg)
            seq.annotations['ambiguous'] = True
        else:
            seq.annotations['ambiguous'] = False

    return seqlist


def repredict_structures_for_homol_seqs(
        query, all_hits_fasta,
        threads=None,
        prediction_method=tuple('alifold_refold',),
        pred_method_params=None,
        all_hits=None,
):
    """
    use some approach to predict as best structures as possible
    some fast alignment and consensus prediction with refold like proces is also possible
    :return:
    """
    print('entering structure prediction')

    default_sim_tr_perc = 90
    default_score_tr = 0.0
    query_max_len_diff = 0.1
    # output are structures for all_hits
    all_hits_list = [i.subs[i.ret_keys[0]] for i in all_hits]

    all_hits_list = annotate_ambiguos_bases(all_hits_list)
    query = annotate_ambiguos_bases([query])[0]

    # The FIRST sequence in the list is QUERY!!!!
    # query = homologous_seqs[0]
    # the clustering etc is part of consensus style prediction
    # alifold is susceptible if sequences are not very different
    # select most different sequences - cluster sequences, go for x identity
    # combine eval with locarna score

    if not isinstance(pred_method_params, dict):
        raise Exception('prediction method parameters must be python dict')

    # prediction_method = 'alifold_refold'
    structures = dict()
    exec_time = dict()

    if 'default' in prediction_method:
        # do nothing
        pass

    if 'rfam_rnafoldc' in prediction_method:
        pkey = 'rfam_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        # select cm_model ()
        fd, temp_query_file = mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write('>{}\n{}\n'.format(query.id, str(query.seq)))

        if pkey in pred_method_params and pred_method_params[pkey]:
            best_model = get_cm_model(temp_query_file, params=pred_method_params[pkey], threads=threads)
        else:
            best_model = get_cm_model(temp_query_file, threads=threads)

        rfam = RfamInfo()
        single_cm_file = run_cmfetch(rfam.file_path, best_model)

        os.remove(temp_query_file)

        if pkey in pred_method_params and pred_method_params[pkey]:
            structures[pkey], exec_time[pkey] = cmmodel_rnafold_c(
                all_hits_fasta,
                single_cm_file,
                threads=threads,
                params=pred_method_params[pkey]
            )
        else:
            structures[pkey], exec_time[pkey] = cmmodel_rnafold_c(
                all_hits_fasta,
                single_cm_file,
                threads=threads
            )

        os.remove(single_cm_file)
        del pkey

    if 'rfam_subopt' in prediction_method:
        pkey = 'rfam_subopt'
        BA_support.devprint(pkey, flush=True)
        fd, temp_query_file = mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write('>{}\n{}\n'.format(query.id, str(query.seq)))

        if pkey in pred_method_params and pred_method_params[pkey]:
            structures[pkey], exec_time[pkey] = rfam_subopt_pred(
                all_hits_fasta,
                query_file=temp_query_file,
                threads=threads,
                params=pred_method_params[pkey]
            )
        else:
            structures[pkey], exec_time[pkey] = rfam_subopt_pred(
                all_hits_fasta,
                query_file=temp_query_file,
                threads=threads
            )
        os.remove(temp_query_file)
        del pkey

    if 'rfam_rapidshapes' in prediction_method:
        pkey = 'rfam_rapidshapes'
        BA_support.devprint(pkey, flush=True)
        print(pkey)

        fd, temp_query_file = mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write('>{}\n{}\n'.format(query.id, str(query.seq)))

        if pkey in pred_method_params and pred_method_params[pkey]:
            structures[pkey], exec_time[pkey] = cmscan_rapidshapes(
                all_hits_fasta,
                temp_query_file,
                params=pred_method_params[pkey],
                threads=threads
            )
        else:
            structures[pkey], exec_time[pkey] = cmscan_rapidshapes(
                all_hits_fasta,
                temp_query_file,
                threads=threads
            )

        os.remove(temp_query_file)
        del pkey
        del fd
        del temp_query_file

    if 'clustalo_alifold_rapidshapes' in prediction_method:
        pkey = 'clustalo_alifold_rapidshapes'
        BA_support.devprint(pkey, flush=True)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    pred_method_params[pkey],
                    threads=threads,
                    msa_alg='clustalo'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    threads=threads,
                    msa_alg='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'muscle_alifold_rapidshapes' in prediction_method:
        pkey = 'muscle_alifold_rapidshapes'
        BA_support.devprint(pkey, flush=True)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    pred_method_params[pkey],
                    threads=threads,
                    msa_alg='muscle'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    threads=threads,
                    msa_alg='muscle'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'rcoffee_alifold_rapidshapes' in prediction_method:
        pkey = 'rcoffee_alifold_rapidshapes'
        BA_support.devprint(pkey, flush=True)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    pred_method_params[pkey],
                    threads=threads,
                    msa_alg='rcoffee'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = msa_alifold_rapidshapes(
                    all_hits_fasta,
                    nr_homo_hits_file,
                    threads=threads,
                    msa_alg='rcoffee'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'alifold_refold' in prediction_method:
        pkey = 'alifold_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='clustalo'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    msa_alg='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'muscle_alifold_refold' in prediction_method:
        pkey = 'muscle_alifold_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file, all_hits_fasta, refold='refold', threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='muscle'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file, all_hits_fasta, refold='refold', threads=threads,
                    msa_alg='muscle'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'rnafold' in prediction_method:
        pkey = 'rnafold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        if pkey in pred_method_params and pred_method_params[pkey]:
            structures[pkey], exec_time[pkey] = rnafold_prediction(
                all_hits_fasta,
                params=pred_method_params[pkey].get('RNAfold', '')
            )
        else:
            structures[pkey], exec_time[pkey] = rnafold_prediction(
                all_hits_fasta
            )
        del pkey

    if 'subopt_fold_query' in prediction_method:
        pkey = 'subopt_fold_query'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        if pkey in pred_method_params and pred_method_params[pkey]:
            structures[pkey], exec_time[pkey] = subopt_fold_query(
                all_hits_fasta,
                query,
                params=pred_method_params[pkey]
            )
        else:
            structures[pkey], exec_time[pkey] = subopt_fold_query(
                all_hits_fasta,
                query
            )

        del pkey

    if 'subopt_fold_clustal_alifold' in prediction_method:
        pkey = 'subopt_fold_clustal_alifold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )

            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file

            f, homologous_sequence_file = mkstemp()
            with os.fdopen(f, 'w') as fh:
                BA_support.write_fasta_from_list_of_seqrecords(fh, homologous_seqs)

            if pkey in pred_method_params and pred_method_params[pkey]:
                structures[pkey], exec_time[pkey] = subopt_fold_alifold(
                    all_hits_fasta,
                    homologous_sequence_file,
                    aligner='clustalo',
                    params=pred_method_params[pkey],
                    threads=threads
                )
            else:
                structures[pkey], exec_time[pkey] = subopt_fold_alifold(
                    all_hits_fasta,
                    homologous_sequence_file,
                    aligner='clustalo',
                    threads=threads
                )

            os.remove(homologous_sequence_file)
            del homologous_sequence_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'subopt_fold_muscle_alifold' in prediction_method:
        pkey = 'subopt_fold_muscle_alifold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )

            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file

            f, homologous_sequence_file = mkstemp()
            with os.fdopen(f, 'w') as fh:
                BA_support.write_fasta_from_list_of_seqrecords(fh, homologous_seqs)

            if pkey in pred_method_params and pred_method_params[pkey]:
                structures[pkey], exec_time[pkey] = subopt_fold_alifold(
                    all_hits_fasta,
                    homologous_sequence_file,
                    aligner='muscle',
                    params=pred_method_params[pkey],
                    threads=threads,
                )
            else:
                structures[pkey], exec_time[pkey] = subopt_fold_alifold(
                    all_hits_fasta,
                    homologous_sequence_file,
                    aligner='muscle',
                    threads=threads,
                )

            os.remove(homologous_sequence_file)
            del homologous_sequence_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'alifold_refold_rnafold_c' in prediction_method:
        pkey = 'alifold_refold_rnafold_c'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='clustalo'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    msa_alg='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'muscle_alifold_refold_rnafold_c' in prediction_method:
        pkey = 'muscle_alifold_refold_rnafold_c'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='muscle'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    msa_alg='muscle'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'alifold_unpaired_conserved_refold' in prediction_method:
        pkey = 'alifold_unpaired_conserved_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='clustalo'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    msa_alg='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'muscle_alifold_unpaired_conserved_refold' in prediction_method:
        pkey = 'muscle_alifold_unpaired_conserved_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    msa_alg='muscle'
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = alifold_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    msa_alg='muscle'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_tcoffee_alifold_refold' in prediction_method:
        pkey = 'dh_tcoffee_alifold_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_tcoffee_alifold_refold_rnafoldc' in prediction_method:
        pkey = 'dh_tcoffee_alifold_refold_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_tcoffee_alifold_conserved_ss_rnafoldc' in prediction_method:
        pkey = 'dh_tcoffee_alifold_conserved_ss_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_clustal_alifold_refold' in prediction_method:
        pkey = 'dh_clustal_alifold_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    params=pred_method_params[pkey],
                    align='clustalo'
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    align='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_clustal_alifold_refold_rnafoldc' in prediction_method:
        pkey = 'dh_clustal_alifold_refold_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    align='clustalo'
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    align='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'dh_clustal_alifold_conserved_ss_rnafoldc' in prediction_method:
        pkey = 'dh_clustal_alifold_conserved_ss_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey],
                    align='clustalo'
                )
            else:
                nr_homo_hits_file, homologous_seqs = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = decouple_homologs_alifold_refold_prediction(
                    nr_homo_hits_file,
                    homologous_seqs,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    align='clustalo'
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
            del homologous_seqs
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'pairwise_centroid_homfold' in prediction_method:
        pkey = 'pairwise_centroid_homfold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        if pkey in pred_method_params and pred_method_params[pkey]:
            nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_unsafe(
                all_hits=all_hits_list,
                query=query,
                sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
            )
        else:
            nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_unsafe(
                all_hits=all_hits_list,
                query=query,
                sim_threshold_percent=default_sim_tr_perc,
                cmscore_tr=default_score_tr,
                len_diff=query_max_len_diff,
            )

        raw_structures, exec_time[pkey] = me_centroid_homfold(
            all_hits_fasta, nr_homo_hits_file,
            params=pred_method_params.get(pkey, None)
        )

        # check noncanonical
        if pkey in pred_method_params and pred_method_params[pkey]:
            allow_nc = pred_method_params[pkey].get('allow_noncanonical', False)
            allow_lp = pred_method_params[pkey].get('allow_lonely_pairs', False)
        else:
            allow_nc = False
            allow_lp = False
        if not allow_nc:
            for seq in raw_structures:
                repstr = find_nc_and_remove(str(seq.seq), structure=seq.letter_annotations['ss0'])
                seq.letter_annotations['ss0'] = repstr

        # check lonely basepairs
        if not allow_lp:
            for seq in raw_structures:
                repstr = check_lonely_bp(seq.letter_annotations['ss0'])
                seq.letter_annotations['ss0'] = repstr

        structures[pkey] = raw_structures
        os.remove(nr_homo_hits_file)
        del nr_homo_hits_file
        del pkey

    if 'TurboFold_conservative' in prediction_method:
        pkey = 'TurboFold_conservative'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        if pkey in pred_method_params and pred_method_params[pkey]:
            nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_unsafe(
                all_hits=all_hits_list,
                query=query,
                sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
            )
        else:
            nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_unsafe(
                all_hits=all_hits_list,
                query=query,
                sim_threshold_percent=default_sim_tr_perc,
                cmscore_tr=default_score_tr,
                len_diff=query_max_len_diff,
            )

        checked_hits = filter_ambiguous_seqs_from_list(all_hits_list)
        ch_fd, all_checked_hits_file = mkstemp()
        with os.fdopen(ch_fd, 'w') as ch:
            SeqIO.write(checked_hits, ch, format='fasta')

        structures_t, exec_time[pkey] = turbofold_conservative_prediction(all_checked_hits_file, nr_homo_hits_file)

        structures[pkey] = BA_support.rebuild_structures_output_from_pred(
            all_hits_list,
            structures_t
        )

        os.remove(all_checked_hits_file)
        os.remove(nr_homo_hits_file)
        del structures_t
        del ch_fd
        del nr_homo_hits_file
        del pkey

    if 'TurboFold' in prediction_method:
        pkey = 'TurboFold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        # set arbitrary sim_threshold_percent to 100, because we want to remove only identical sequences from prediction
        #  with Trurbofold. The structure of redundant sequences will be set according to the one in prediction
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=100,
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    check_unambiguous=True,
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )

            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=100,
                    cmscore_tr=default_score_tr,
                    check_unambiguous=True,
                    len_diff=query_max_len_diff,
                )

            checked_hits = filter_ambiguous_seqs_from_list(all_hits_list)
            with open(nr_homo_hits_file, 'r') as nrf:
                nr_homo_hits = [seq for seq in SeqIO.parse(nrf, format='fasta')]

            structures_t, exec_time[pkey] = turbofold_only_homologous(
                all_sequences=checked_hits,
                nr_homologous=nr_homo_hits,
                params=pred_method_params.get(pkey, {}).get('TurboFold', {})
            )

            structures[pkey] = BA_support.rebuild_structures_output_from_pred(
                all_hits_list,
                structures_t
            )

            os.remove(nr_homo_hits_file)
            del structures_t
            del nr_homo_hits
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'tcoffee_rcoffee_alifold_refold' in prediction_method:
        pkey = 'tcoffee_rcoffee_alifold_refold'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold',
                    threads=threads,
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'tcoffee_rcoffee_alifold_refold_rnafoldc' in prediction_method:
        pkey = 'tcoffee_rcoffee_alifold_refold_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='refold_rnafoldc',
                    threads=threads,
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    if 'tcoffee_rcoffee_alifold_conserved_ss_rnafoldc' in prediction_method:
        pkey = 'tcoffee_rcoffee_alifold_conserved_ss_rnafoldc'
        BA_support.devprint(pkey, flush=True)
        print(pkey)
        try:
            if pkey in pred_method_params and pred_method_params[pkey]:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=pred_method_params[pkey].get('pred_sim_threshold', default_sim_tr_perc),
                    cmscore_tr=pred_method_params[pkey].get('cmscore_tr', default_score_tr),
                    cm_threshold_percent=pred_method_params[pkey].get('cmscore_percent', None),
                    len_diff=pred_method_params[pkey].get('query_max_len_diff', query_max_len_diff),
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                    params=pred_method_params[pkey]
                )
            else:
                nr_homo_hits_file, _ = create_nr_homolog_hits_file_MSA_safe(
                    all_hits=all_hits_list,
                    query=query,
                    sim_threshold_percent=default_sim_tr_perc,
                    cmscore_tr=default_score_tr,
                    len_diff=query_max_len_diff,
                )
                structures[pkey], exec_time[pkey] = tcoffee_rcoffee_refold_prediction(
                    nr_homo_hits_file,
                    all_hits_fasta,
                    refold='conserved_ss_rnafoldc',
                    threads=threads,
                )
            os.remove(nr_homo_hits_file)
            del nr_homo_hits_file
        except NoHomologousSequenceException:
            nonhomseqwarn(pkey)
        finally:
            del pkey

    return structures, exec_time