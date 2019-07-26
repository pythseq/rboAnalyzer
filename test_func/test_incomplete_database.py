import os
import unittest
from test_func.pseudoargs_class import Pseudoargs
from rna_blast_analyze.BR_core.expand_by_LOCARNA import locarna_anchored_wrapper_inner

fwd = os.path.dirname(__file__)
test_dir = 'test_data'


class TestIncompleteDatabase(unittest.TestCase):
    def test_if_missing_db_entry_is_reported(self):
        aa = Pseudoargs(
            blast_query=os.path.join(fwd, test_dir, 'RF00005_query.fasta'),
            blast_in=os.path.join(fwd, test_dir, 'RF00005.blastout'),
            blast_db=os.path.join(fwd, test_dir, 'blastdb', 'RF00001-art.blastdb'),
            b_type='plain',
            prediction_method=['rnafold'],
            blast_regexp='(?<=\|)[A-Z0-9]*\.?\d*$',
            enable_overwrite=True
        )

        with self.assertRaises(LookupError):
            locarna_anchored_wrapper_inner(aa)
