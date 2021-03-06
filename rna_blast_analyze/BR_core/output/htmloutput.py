import os
import json
import logging
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.exceptions import TemplateError
from time import strftime
import re

from rna_blast_analyze.BR_core.BA_support import blasthsp2pre, remove_one_file_with_try
from rna_blast_analyze.BR_core.viennaRNA import run_rnaplot
from rna_blast_analyze.BR_core.config import CONFIG
from rna_blast_analyze.BR_core.exceptions import RNAplotException

from matplotlib import colors, cm

ml = logging.getLogger(__name__)


def rog_cmap(colcodes):
    cmap_rog = colors.LinearSegmentedColormap.from_list(
        name='rog',
        colors=[colors.hex2color(c) for c in colcodes],
        N=1024
    )
    return cmap_rog


reference_colors = {
    'Homologous': '#7AD84B',
    'Not homologous': '#E24B2D'
}


def write_html_output(datain, template_path=''):
    ml.info("Writing HTML output.")
    # prepare data
    toprint = _prepare_body(datain)
    myfooter = _prepare_footer(datain)
    my_header = _prepare_header(datain)

    # init jinja2 rendering environment
    env = Environment(
        loader=FileSystemLoader(template_path),
        autoescape=select_autoescape(['html', 'xml'])
    )
    cwd = os.getcwd()
    try:
        os.chdir(CONFIG.html_template_dir)
        template = env.get_template('onehit.html')
        html_str = template.render(
            input_list=toprint,
            foo=myfooter,
            strftime=strftime,
            hea=my_header,
            show_gene_browser=datain.args.show_gene_browser,
            len=len,
        )
        return html_str.encode('utf8')
    except TemplateError:
        ml.error("Jinja rendering error. Please check if the template is available and correct.")
        raise
    except Exception:
        ml.error("Failed to render html.")
        raise
    finally:
        os.chdir(cwd)


def _prepare_header(data):
    return {
        'input': data.args.blast_in,
        'query': data.args.blast_query,
        'best_matching_model': data.best_matching_model,
    }


def _prep_hit_name(id, desc):
    if desc.startswith(id):
        return desc
    else:
        return id + " " + desc


def _prepare_body(data):
    rog = rog_cmap(['#9E9414', reference_colors['Homologous']])
    norm = colors.Normalize(vmin=0, vmax=len(data.query.seq)*0.7, clip=True)
    mm = cm.ScalarMappable(norm=norm, cmap=rog)

    # rebuild original order of hits in case there was missing one:
    d = dict()
    for h in data.hits + data.hits_failed:
        key = int(re.split('[|:]', h.source.id)[1])
        d[key] = h

    records2draw = []
    for key in sorted(d.keys()):
        records2draw.append(d[key])

    jj = []
    for i, onehit in enumerate(records2draw):
        rr = dict()
        rr['source_seq_name'] = onehit.source.annotations['blast'][0]
        rr['blast_hit_name'] = _prep_hit_name(onehit.source.annotations['blast'][0], onehit.source.description)
        rr['blast_text'] = blasthsp2pre(onehit.source.annotations['blast'][1])
        rr['eval'] = onehit.source.annotations['blast'][1].expect
        rr['intid'] = str(i)
        rr['msgs'] = set(onehit.source.annotations['msgs'])
        if onehit.extension is not None:
            rr['msgs'] |= set(onehit.extension.annotations['msgs'])

        lx = len(data.query.seq)

        seqview = [
            'embedded=true',
            '&noviewheader=true',
            '&id={}'.format(onehit.source.annotations['blast'][0]),
            '&appname=rboAnalyzer',
            '&multipanel=false',
            '&slim=true'
        ]
        sviewlink = [
            'https://www.ncbi.nlm.nih.gov/nuccore/',
            '{}'.format(onehit.source.annotations['blast'][0]),
            '?report=graph',
        ]

        if onehit.extension is not None:
            ext = onehit.extension
            h_bit_sc = ext.annotations['cmstat']['bit_sc']

            rr['seqname'] = ext.id
            rr['sequence'] = str(ext.seq)
            rr['formated_seq'] = ext.format('fasta')
            rr['rsearchbitscore'] = h_bit_sc
            rr['ext_start'] = onehit.best_start
            rr['ext_end'] = onehit.best_end
            rr['pictures'] = _prepare_pictures(ext)
            rr['h_estimate'] = ext.annotations['homology_estimate']

            if ext.annotations['homology_estimate'] == 'Uncertain':
                rr['h_color'] = colors.rgb2hex(mm.to_rgba(h_bit_sc))
                rr['estimate_pointer'] = u' ↴'
            else:
                rr['h_color'] = reference_colors[ext.annotations['homology_estimate']]

            # ==== markers ====
            extended_marker = ['&mk={}:{}|BestMatch!'.format(onehit.best_start, onehit.best_end)]

            seqview += extended_marker
            sviewlink += extended_marker

            if data.args.show_HSP:
                br = onehit.source.annotations['blast'][1]
                if br.sbjct_start < br.sbjct_end:
                    bs = br.sbjct_start
                    be = br.sbjct_end
                else:
                    bs = br.sbjct_end
                    be = br.sbjct_start
                hsp_marker = ['&mk={}:{}|HSP!'.format(bs, be)]

                seqview += hsp_marker
                sviewlink += hsp_marker

        else:
            rr['h_color'] = reference_colors['Not homologous']

        # create seqviewurl here
        es = onehit.source.annotations['extended_start']
        ee = onehit.source.annotations['extended_end']
        if es > ee:
            es, ee = [ee, es]

        diff = 1000 + 2*lx
        es -= diff
        ee += diff

        if es < 0:
            es = 1

        position = ['&v={}:{}'.format(es, ee)]
        seqview += position
        sviewlink += position

        rr['seqviewurl'] = ''.join(seqview)
        rr['seqvid'] = 'seqv_{}'.format(i)
        rr['seqviewlink'] = ''.join(sviewlink)

        jj.append(rr)
    return jj


def _prepare_pictures(sub):
    pictureslist = []
    picfile = None
    for key in sub.letter_annotations.keys():
        np = dict()
        np['picname'] = key
        np['secondary_structure'] = sub.letter_annotations[key]

        try:
            picfile = run_rnaplot(
                seq=str(sub.seq),
                structure=sub.letter_annotations[key],
                format='svg'
            )
            with open(picfile) as f:
                np['pic'] = "data:image/svg+xml;utf8," + f.read()

            pictureslist.append(np)

            remove_one_file_with_try(picfile)
        except RNAplotException:
            print("can't draw structure with RNAfold for {}.".format(sub.id))
        except FileNotFoundError:
            if picfile is not None:
                print('cannot remove file: {}, file not found'.format(picfile))
        except OSError:
            if picfile is not None:
                print('cannot remove file: {}, file is directory'.format(picfile))

    return pictureslist


def _prepare_footer(data):
    # write command
    if hasattr(data.args, 'command') and data.args.command:
        command = ' '.join(data.args.command)
    else:
        command = 'run directly from python'

    # parameters
    params = [i for i in dir(data.args) if not i.startswith('__') and not callable(getattr(data.args, i))]
    p_text = []
    for arg in params:
        if arg == 'pred_params':
            preprocessed_pred_params = {k: v for k, v in getattr(data.args, arg).items() if k in data.args.prediction_method}
            js = json.dumps(
                preprocessed_pred_params,
                sort_keys=True,
                indent=4,
            )
            p_text.append((arg, js[1:-1]))
            continue
        p_text.append((arg, getattr(data.args, arg)))

    if not hasattr(data, 'date_of_run'):
        data.date_of_run = None

    prepared_footer_data = {
        'command': command,
        'parameters': p_text,
        'exec_date': data.date_of_run,
        'logdup': data.msgs
    }
    return prepared_footer_data
