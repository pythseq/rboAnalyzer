import binascii
import pickle
import time
import copy
from argparse import Namespace

from Bio.Blast import Record
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from pandas import read_json

import rna_blast_analyze.BR_core.BA_methods
from rna_blast_analyze.BR_core.BA_support import Subsequences


def seq2dict(seq):
    if not isinstance(seq, Seq):
        raise TypeError('accepts only Bio.Seq')
    out = {
        'seq': str(seq),
        'alphabet': binascii.hexlify(pickle.dumps(seq.alphabet)).decode()
    }
    return out


def seqfromdict(indict):
    return Seq(
        indict['seq'],
        alphabet=pickle.loads(
            binascii.unhexlify(
                indict['alphabet']
            )
        )
    )


def seqrecord2dict(seqrec):
    """
    using pickle for features (usually not used)
     Bio.SeqFeature.SeqFeature class with Bio.SeqFeature.FeatureLocation should be used
     - implement direct conversion if needed
    :param seqrec:
    :return:
    """
    if not isinstance(seqrec, SeqRecord):
        raise TypeError('accepts only Bio.SeqRecord')
    out = {
        'Bio.Seq': seq2dict(seqrec.seq),
        'annotations': annotations_items2dict(seqrec.annotations),
        'dbxrefs': seqrec.dbxrefs,
        'description': seqrec.description,
        'features': [binascii.hexlify(pickle.dumps(ff)).decode() for ff in seqrec.features],
        'id': seqrec.id,
        'name': seqrec.name,
        'letter_annotations': seqrec.letter_annotations,
    }
    return out


def seqrecordfromdict(indict):
    return SeqRecord(
        seqfromdict(indict['Bio.Seq']),
        annotations=annotations_items_from_dict(indict['annotations']),
        dbxrefs=indict['dbxrefs'],
        description=indict['description'],
        features=[pickle.loads(binascii.unhexlify(ff)) for ff in indict['features']],
        id=indict['id'],
        name=indict['name'],
        letter_annotations=indict['letter_annotations'],
    )


def annotations_items2dict(item):
    out = item.copy()
    if 'blast' in item:
        out['blast'] = (item['blast'][0], hsptodict(item['blast'][1]))
    if 'cmstat' in item:
        out['cmstat'] = item['cmstat'].to_json()
    return out


def annotations_items_from_dict(indict):
    if 'blast' in indict:
        indict['blast'] = (indict['blast'][0], hspfromdict(indict['blast'][1]))
    if 'cmstat' in indict:
        indict['cmstat'] = read_json(indict['cmstat'], typ='series')
    return indict


def subsequences2dict(subsequences):
    if not isinstance(subsequences, Subsequences):
        raise TypeError('accepts only BA_support.Subsequences')
    out = {
        'subs': {key: seqrecord2dict(subsequences.subs[key]) for key in subsequences.subs.keys()},
        'source': seqrecord2dict(subsequences.source),
        'ret_keys': subsequences.ret_keys,
        'query_name': subsequences.query_name,
        'best_start': subsequences.best_start,
        'best_end': subsequences.best_end,
        'templates': subsequences.templates,
    }
    return out


def subsequencesfromdict(indict):
    out = Subsequences(
        seqrecordfromdict(indict['source'])
    )
    for key in indict['subs'].keys():
        out.subs[key] = seqrecordfromdict(indict['subs'][key])
    out.ret_keys = indict['ret_keys']
    out.query_name = indict['query_name']
    out.best_start = indict['best_start']
    out.best_end = indict['best_end']
    out.templates = indict['templates']
    return out


def hitlist2dict(hl):
    if not isinstance(hl, rna_blast_analyze.BR_core.BA_methods.HitList):
        raise TypeError('Accepts only BA_methods.HitList. {} given.'.format(type(hl)))
    out = {}
    for i in range(len(hl)):
        out[str(i)] = subsequences2dict(hl[i])
    return out


def hitlistfromdict(indict):
    out = rna_blast_analyze.BR_core.BA_methods.HitList()
    for i in range(len(indict)):
        out.append(subsequencesfromdict(indict[str(i)]))
    return out


def blastsearchrecompute2dict(bsr):
    if not isinstance(bsr, rna_blast_analyze.BR_core.BA_methods.BlastSearchRecompute):
        raise TypeError('Accepts only BA_methods.BlastSearchRecompute. {} given'.format(type(bsr)))
    out = {
        'hits': hitlist2dict(bsr.hits),
        'query': seqrecord2dict(bsr.query),
        'creation': bsr.creation,
        '_runstat': bsr._runstat,
        'args': vars(bsr.args),
        'date_of_run': list(bsr.date_of_run)
    }
    return out


def blastsearchrecomputefromdict(indict):
    out = rna_blast_analyze.BR_core.BA_methods.BlastSearchRecompute()
    out.hits = hitlistfromdict(indict['hits'])
    out.query = seqrecordfromdict(indict['query'])
    out.creation = indict['creation']
    out._runstat = indict['_runstat']
    out.args = Namespace(**indict['args'])
    out.date_of_run = time.struct_time(indict['date_of_run'])
    return out


def headertodict(header):
    if not isinstance(header, Record.Header):
        raise TypeError('accepts only BlastRecord.Header')
    return vars(header)


def headerfromdict(indict):
    return universalfromdict(indict, Record.Header)


def hsptodict(hsp):
    if not isinstance(hsp, Record.HSP):
        raise TypeError('accepts only BlastRecord.HSP')
    return vars(hsp)


def hspfromdict(indict):
    return universalfromdict(indict, Record.HSP)


def blastalignmenttodict(blastalig):
    if not isinstance(blastalig, Record.Alignment):
        raise TypeError('accepts only Bio.Blast.Record.Alignment')
    out = vars(copy.copy(blastalig))
    out['hsps'] = [hsptodict(h) for h in blastalig.hsps]
    return out


def blastalignmentfromdict(indict):
    out = Record.Alignment()
    keys = set(indict.keys())
    keys.remove('hsps')
    for key in keys:
        setattr(out, key, indict[key])
    out.hsps = [hspfromdict(h) for h in indict['hsps']]
    return out


def blasttodict(blast):
    if not isinstance(blast, Record.Blast):
        raise TypeError('accepts only BlastRecord.Blast')

    out = dict()
    for key in vars(blast).keys():
        if key == 'descriptions':
            out[key] = [vars(d) for d in blast.descriptions]
        elif key == 'alignments':
            out[key] = [blastalignmenttodict(balig) for balig in blast.alignments]
        elif key == 'multiple_alignments':
            out[key] = []
        else:
            out[key] = getattr(blast, key)
    return out


def blastfromdict(indict):
    out = Record.Blast()
    for key in indict.keys():
        if key == 'descriptions':
            out.descriptions = [universalfromdict(d, Record.Description) for d in indict['descriptions']]
        elif key == 'alignments':
            out.alignments = [blastalignmentfromdict(balig) for balig in indict['alignments']]
        else:
            setattr(out, key, indict[key])
    return out


def universalfromdict(inputdict, outclasspointer):
    outputobject = outclasspointer()
    for key in inputdict.keys():
        setattr(outputobject, key, inputdict[key])

    return outputobject
