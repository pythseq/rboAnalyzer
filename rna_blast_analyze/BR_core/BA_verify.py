import logging
import os
import re
import sys
import operator
from subprocess import check_output, STDOUT, CalledProcessError

from rna_blast_analyze.BR_core import cmalign
from rna_blast_analyze.BR_core.config import CONFIG
from rna_blast_analyze.BR_core.tools_versions import blast_minimal_version, locarna_minimal_version, \
    infernal_minimal_version, vrna_minimal_version, clustalo_minimal_version, muscle_minimal_version, \
    centroid_homfold_minimal_version, turbofold_minimal_version,\
    mfold_minimal_version, method_required_tools, blast_maximal_version, locarna_maximal_version

ml = logging.getLogger('rboAnalyzer')


def verify_blastdbcmd(minimal_version, maximal_version):
    """verify if blastdbcmd is present in supported version
    """
    msgversion = 'blastcmd not installed in required version, required version is between {}.{}.{} and {}.{}.{}'.format(*minimal_version + maximal_version)
    msgpath = '{}blastcmd could not be located (not in PATH)'.format(CONFIG.blast_path)
    msgsuccess = 'blastcmd is installed in required version'
    try:
        a = check_output(
            [
                '{}blastdbcmd'.format(CONFIG.blast_path),
                '-version'
            ]
        )
        a = a.decode(encoding='utf-8')
        r = re.search('(?<=blastdbcmd: )[0-9.]+', a)
        if r:
            ver = r.group().split('.')
            ver = [int(i) for i in ver]
            bb_min = version_check(ver, minimal_version, msgsuccess, msgversion)
            bb_max = version_check(ver, maximal_version, msgsuccess, msgversion, op=operator.lt)
            if bb_min and bb_max:
                return True
            else:
                return False
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_locarna(minimal_version, maximal_version):
    msgversion = 'Locarna is not installed in required version, required version is {}.{}.{}'.format(*locarna_minimal_version)
    msgpath = '{}LocARNA could not be located (not in PATH).'.format(CONFIG.locarna_path)
    msgsuccess = 'Locarna is installed in required version'
    try:
        a = check_output(
            [
                '{}locarna'.format(CONFIG.locarna_path),
                '--version'
            ]
        )
        a = a.decode()
        if a.startswith('LocARNA'):
            r = [int(m.group()) for m in re.finditer('[0-9]+', a)]
            bb_min = version_check(r, minimal_version, msgsuccess, msgversion)
            bb_max = version_check(r, maximal_version, msgsuccess, msgversion, operator.lt)
            if bb_min and bb_max:
                return True
            else:
                return False
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_infernal(program, minimal_version):
    msgversion = '{} (part of INFERNAL) is not installed in required version, required is {}.{}.{}'.format(program, *minimal_version)
    msgpath = '{}{} could not be located (not in PATH).'.format(CONFIG.infernal_path, program)
    msgsuccess = '{} is installed in required version'.format(program)
    try:
        a = check_output(
            [
                CONFIG.infernal_path + program,
                '-h'
            ]
        )
        a = a.decode()
        if a.startswith('# {}'.format(program)):
            r = re.search('(?<=# INFERNAL )[0-9.]+', a)
            ver = r.group().split('.')
            ver = [int(i) for i in ver]
            return version_check(ver, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_viennarna_program(program, minimal_version):
    msgversion = '{} is not installed in required version, required version is {}.{}.{}'.format(program, *minimal_version)
    msgpath = '{}{} could not be located'.format(CONFIG.viennarna_path, program)
    msgsuccess = '{} is installed in required version'.format(program)
    try:
        a = check_output(
            [
                CONFIG.viennarna_path + program,
                '--version'
            ]
        )
        a = a.decode()
        if a.startswith(program):
            ver = [int(i) for i in a.split()[1].split('.')]
            return version_check(ver, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_viennarna(programs, vrna_minv):
    installed = set()
    for prog in programs:
        if verify_viennarna_program(prog, vrna_minv):
            installed.add(prog)
    return installed


def verify_vrna_refold():
    msgversion = 'refold.pl is not installed in required version.'
    msgpath = '{}refold.pl could not be located (not in PATH).'.format(CONFIG.refold_path)
    msgsuccess = 'refold.pl is instaled in required version'
    try:
        try:
            a = check_output(
                ['{}refold.pl'.format(CONFIG.refold_path), '-h'],
                stderr=STDOUT,
            )
        except CalledProcessError as e:
            a = e.output

        a = a.decode()
        if a.strip().startswith('refold.pl [-t threshold] myseqs.aln alidot.ps | RNAfold -C'):
            ml.info(msgsuccess)
            return True
        else:
            ml.info(msgversion)
            return False
    except FileNotFoundError:
        ml.info(msgpath)
        return False


def verify_clustalo(minimal_version):
    msgversion = 'clustalo is not installed in required version, required version is {}.{}.{}'.format(*minimal_version)
    msgpath = '{}clustalo could not be located (not in PATH).'.format(CONFIG.clustal_path)
    msgsuccess = 'clustalo is installed in required version'
    try:
        a = check_output(
            [
                '{}clustalo'.format(CONFIG.clustal_path),
                '--version'
            ]
        )
        a = a.decode()
        if a:
            r = [int(m.group()) for m in re.finditer('[0-9]+', a)]
            return version_check(r, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_muscle(minimal_version):
    msgversion = 'muscle is not installed in required version, required version is {}.{}.{}'.format(*minimal_version)
    msgpath = '{}muscle could not be located (not in PATH).'.format(CONFIG.muscle_path)
    msgsuccess = 'muslce is installed in required version'
    try:
        a = check_output(
            [
                '{}muscle'.format(CONFIG.muscle_path),
                '-version'
            ]
        )
        a = a.decode()
        if a.startswith('MUSCLE'):
            r = [int(m.group()) for m in re.finditer('[0-9]+', a)]
            return version_check(r, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_centroid_homfold(minimal_version):
    msgversion = 'centroid_homfold is not installed in required version, required version is {}.{}.{}'.format(*minimal_version)
    msgpath = '{}centroid_homfold could not be located (not in PATH).'.format(CONFIG.centriod_path)
    msgsuccess = 'centroid_homfold is installed in required version'
    # double try because help returns exit status 1
    try:
        try:
            a = check_output(
                [
                    '{}centroid_homfold'.format(CONFIG.centriod_path),
                    '-h'
                ],
                stderr=STDOUT
            )
        except CalledProcessError as e:
            a = e.output

        a = a.decode()
        b = a.split()
        if b[0] == 'CentroidHomfold':
            r = [int(m.group()) for m in re.finditer('[0-9]+', b[1])]
            return version_check(r, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_turbofold(minimal_version):
    msgversion = 'TurboFold is not installed in required version, required version is {}.{}'.format(*minimal_version)
    msgpath = '{}TurboFold could not be located (not in PATH).'.format(CONFIG.turbofold_path)
    msgsuccess = 'TruboFold is installed in required version'
    try:
        try:
            a = check_output(
                [
                    '{}TurboFold'.format(CONFIG.turbofold_path),
                    '-v'
                ],
                stderr=STDOUT
            )
        except CalledProcessError as e:
            a = e.output

        a = a.decode()
        b = a.split()
        if b[0] == 'TurboFold:':
            r = [int(m.group()) for m in re.finditer('[0-9]+', b[2])]
            return version_check(r, minimal_version, msgsuccess, msgversion)
        else:
            ml.warning(msgversion)
            return False
    except FileNotFoundError:
        ml.warning(msgpath)
        return False


def verify_turbofold_datapath():
    """
    verify that datapath enviroment variable is set (it is not set by default when installing turbofold from conda)
    :return:
    """
    try:
        dp = os.environ['DATAPATH']
        return True
    except KeyError:
        if CONFIG.rnastructure_datapath is None:
            return False
        else:
            return True


def verify_mfold(minimal_version):
    msgversion = 'hybrid-ss-min (UNAfold) is not installed in required version, required version is {}.{}. ' \
                 'Please see the manual for installation.'.format(*minimal_version)
    msgpath = '{}hybrid-ss-min could not be located (not in PATH). Please see the manual for installation.'.format(CONFIG.mfold_path)
    msgsuccess = 'hybrid-ss-min (UNAfold) is installed in required version'
    try:
        a = check_output(
            [
                '{}hybrid-ss-min'.format(CONFIG.mfold_path),
                '-V'
            ]
        )
        a = a.decode()
        b = a.split()
        if b[0] == 'hybrid-ss-min':
            r = [int(m.group()) for m in re.finditer('[0-9]+', b[2])]
            return version_check(r, minimal_version, msgsuccess, msgversion)
        else:
            ml.info(msgversion)
            return False
    except FileNotFoundError:
        ml.info(msgpath)
        return False


def version_check(r, minimal_version, msgsuccess, msgversion, op=operator.gt):
    """Function for minimal or maximal version checking

    On version match returns True

    :param r: iterable of version descriptors
    :param minimal_version: iterable of version descriptors
    :param msgsuccess: msg to report on success
    :param msgversion: msg to report on fail
    :param op: operator.gt or operator.lt
    :return: bool
    """

    if op not in (operator.gt, operator.lt):
        raise ValueError('Invalid operator, accepting only operator.gt or operator.lt')

    # handle versions with different length
    rv = []
    mv = []
    for v, minv in zip(r, minimal_version):
        rv.append(v)
        mv.append(minv)

        if op(v, minv):
            ml.info(msgsuccess)
            return True

    if rv == mv:
        ml.info(msgsuccess)
        return True

    ml.warning(msgversion)
    return False


def check_3rd_party_tools():
    installed = set()
    if verify_blastdbcmd(blast_minimal_version, blast_maximal_version):
        installed.add('blastdbcmd')

    if verify_locarna(locarna_minimal_version, locarna_maximal_version):
        installed.add('locarna')

    if verify_infernal('cmbuild', infernal_minimal_version):
        installed.add('infernal')

    if verify_infernal('cmalign', infernal_minimal_version):
        installed.add('infernal')

    installed |= verify_viennarna(['RNAfold', 'RNAplot', 'RNAdistance'], vrna_minimal_version)

    return installed


def check_3rd_party_data():
    installed = set()
    if cmalign.check_rfam_present():
        installed.add('rfam')
    return installed


def check_3rd_party_prediction_tools():
    # clustalo
    # alifold
    # refold
    # centroid_homfold
    # TurboFold
    # mfold (hybrid-ss-min)

    installed = set()

    if verify_viennarna(['RNAalifold',], vrna_minimal_version):
        installed.add('RNAalifold')

    if verify_vrna_refold():
        installed.add('refold.pl')

    if verify_clustalo(clustalo_minimal_version):
        installed.add('clustalo')

    if verify_centroid_homfold(centroid_homfold_minimal_version):
        installed.add('centroid_homfold')

    if verify_turbofold(turbofold_minimal_version):
        installed.add('turbofold')

    if verify_mfold(mfold_minimal_version):
        installed.add('mfold')

    if verify_muscle(muscle_minimal_version):
        installed.add('muscle')

    return installed


def find_file(top, file):
    for root, dirs, files in os.walk(top):
        if file in files:
            # exit early
            return root
    return None


def find_dir(top, directory):
    for root, dirs, files in os.walk(top):
        if directory in dirs:
            # exit early
            return root
    return None


def check_necessery_tools(methods):
    avalible_tools = check_3rd_party_tools()
    avalible_tools |= check_3rd_party_data()
    avalible_tools |= check_3rd_party_prediction_tools()

    for met in methods:
        needed = method_required_tools[met] - avalible_tools

        if 'refold.pl' in needed:
            # This solves the issue of refold.pl script not being added to PATH with conda installs

            msgfail = 'refold.pl not found in PATH. ' \
                      'Please add the refold.pl to PATH or add the path to refold.pl to configuration file.'
            is_conda = os.path.exists(os.path.join(sys.prefix, 'conda-meta'))
            if is_conda:
                status = 'STATUS: refold.pl not found in PATH. Trying to find refold.pl in "CONDA_ROOT/share"'
                ml.info(status)
                print(status)

                out = find_file(os.path.join(sys.prefix, 'share'), 'refold.pl')
                if out is not None:
                    op = out + os.sep

                    msg_found = 'STATUS: Found refold.pl in {}\n' \
                                'writing configuration to {}'.format(op, CONFIG.conf_file)
                    ml.info(msg_found)
                    print(msg_found)

                    CONFIG.tool_paths['refold'] = op
                    if 'TOOL_PATHS' not in CONFIG.config_obj:
                        CONFIG.config_obj['TOOL_PATHS'] = {}
                    CONFIG.config_obj['TOOL_PATHS']['refold'] = op
                    with open(CONFIG.conf_file, 'w') as updated_cfh:
                        CONFIG.config_obj.write(updated_cfh)
                    avalible_tools.add('refold.pl')
                    needed.remove('refold.pl')
                else:
                    print('STATUS: attempt to localize refold.pl was not successful.')
                    ml.error(msgfail)
                    sys.exit(1)
            else:
                ml.error(msgfail)
                sys.exit(1)

        if needed:
            msgfail = 'Missing {} (needed for {}).'.format(' '.join(needed), met)
            ml.error(msgfail)
            sys.exit(1)

    if 'TurboFold' in methods or 'Turbo-fast' in methods:
        msgfail = 'Please provide DATAPATH for TurboFold from RNAstructure package. Either as DATAPATH ' \
                  'environment variable or as rnastructure_DATAPATH entry in configuration file - section DATA. ' \
                  'See the manual for mor information.'
        if not verify_turbofold_datapath():
            ml.info('The TurboFold is installed but the DATAPATH environment variable is not set nor present in configuration file.')
            is_conda = os.path.exists(os.path.join(sys.prefix, 'conda-meta'))
            if is_conda:
                print(
                    'STATUS: The DATAPATH environment variable for TurboFold is not set. '
                    'Trying to find required data in "CONDA_ROOT/share".'
                )
                out = find_dir(os.path.join(sys.prefix, 'share'), 'data_tables')

                if out is not None:
                    op = os.path.join(out, 'data_tables')
                    msg_found = 'STATUS: Inferred datapath in {}. writing configuration to {}'.format(op, CONFIG.conf_file)
                    ml.info(msg_found)
                    print(msg_found)

                    CONFIG.data_paths['rnastructure_datapath'] = op
                    if 'DATA' not in CONFIG.config_obj:
                        CONFIG.config_obj['DATA'] = {}
                    CONFIG.config_obj['DATA']['rnastructure_datapath'] = op
                    with open(CONFIG.conf_file, 'w') as updated_cfh:
                        CONFIG.config_obj.write(updated_cfh)
                else:
                    print('STATUS: attempt to localize TurboFold data was not successful.')
                    ml.error(msgfail)
                    sys.exit(1)
            else:
                ml.error(msgfail)
                sys.exit(1)


if __name__ == '__main__':
    print(check_3rd_party_tools())
    print(check_3rd_party_prediction_tools())


