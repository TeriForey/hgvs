# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals
"""
hgvs.normalizer
"""

import copy
import logging

import hgvs.dataproviders.uta
import hgvs.location
import hgvs.parser
import hgvs.posedit
import hgvs.validator
import hgvs.variantmapper

from .exceptions import HGVSDataNotAvailableError, HGVSValidationError, HGVSUnspportedOperationError

try:
    from vgraph.norm import normalize_alleles
except ImportError:
    from .utils.norm import normalize_alleles

logger = logging.getLogger(__name__)


class Normalizer(object):
    """Perform variant normalization
    """

    def __init__(self, hdp=None, direction=3, cross=False, fill=True, alt_aln_method='splign'):
        """Initialize and configure the normalizer

        :param hdp: HGVS Data Provider Interface-compliant instance (see :class:`hgvs.dataproviders.interface.Interface`)
        :param direction: shuffling direction
        :param cross: whether allow the shuffling to cross the exon-intron boundary
        :param fill: fill in nucleotides or strip nucleotides for delins and dup
        :param alt_aln_method: sequence alignment method (e.g., splign, blat)
        """
        assert direction == 3 or direction == 5, "The shuffling direction should be 3 (3' most) or 5 (5' most)."
        self.hdp = hdp
        self.direction = direction
        self.cross = cross
        self.fill = fill
        self.alt_aln_method = alt_aln_method
        self.hm = hgvs.variantmapper.VariantMapper(self.hdp)

    def _get_boundary(self, var):
        """Get the position of exon-intron boundary for current variant
        """
        try:
            if var.type == 'r' or var.type == 'n':
                if self.cross:
                    return 0, float('inf')
                else:
                    #Get genomic sequence access number for this transcript
                    map_info = self.hdp.get_tx_mapping_options(var.ac)
                    if not map_info:
                        raise HGVSDataNotAvailableError("No mapping info available for {ac}".format(ac=var.ac))
                    map_info = [item for item in map_info if item['alt_aln_method'] == self.alt_aln_method]
                    alt_ac = map_info[0]['alt_ac']

                    #Get tx info
                    tx_info = self.hdp.get_tx_info(var.ac, alt_ac, self.alt_aln_method)
                    cds_start = tx_info['cds_start_i']
                    cds_end = tx_info['cds_end_i']

                    #Get exon info
                    exon_info = self.hdp.get_tx_exons(var.ac, alt_ac, self.alt_aln_method)
                    if not exon_info:
                        raise HGVSDataNotAvailableError("No exon structure available for {ac}".format(ac=var.ac))
                    exon_starts = [exon['tx_start_i'] for exon in exon_info]
                    exon_ends = [exon['tx_end_i'] for exon in exon_info]
                    exon_starts.sort()
                    exon_ends.sort()

                    #Find the end pos of the exon where the var locates
                    left = 0
                    right = float('inf')

                    for i in range(0, len(exon_starts)):
                        if var.posedit.pos.start.base - 1 >= exon_starts[
                            i
                        ] and var.posedit.pos.start.base - 1 < exon_ends[i]:
                            break

                    for j in range(0, len(exon_starts)):
                        if var.posedit.pos.end.base - 1 >= exon_starts[j] and var.posedit.pos.end.base - 1 < exon_ends[
                            j
                        ]:
                            break

                    if i != j:
                        raise HGVSUnspportedOperationError(
                            "Unsupported normalization of variants spanning the exon-intron boundary")

                    left = exon_starts[i]
                    right = exon_ends[i]

                    if var.posedit.pos.end.base - 1 < cds_start:
                        right = min(right, cds_start)
                    elif var.posedit.pos.start.base - 1 >= cds_start:
                        left = max(left, cds_start)
                    else:
                        raise HGVSUnspportedOperationError(
                            "Unsupported normalization of variants spanning the UTR-exon boundary")

                    if var.posedit.pos.start.base - 1 >= cds_end:
                        left = max(left, cds_end)
                    elif var.posedit.pos.end.base - 1 < cds_end:
                        right = min(right, cds_end)
                    else:
                        raise HGVSUnspportedOperationError(
                            "Unsupported normalization of variants spanning the exon-UTR boundary")

                    return left, right
            else:
                #For variant type of g and m etc.
                return 0, float('inf')

        except TypeError:
            raise HGVSDataNotAvailableError("No sequence available for {ac}".format(ac=var.ac))

    def _fetch_bounded_seq(self, var, start, end, boundary):
        """Fetch reference sequence from hgvs data provider.

        The start posotion is 0 and the interval is half open
        """

        start = start if start >= boundary[0] else boundary[0]
        end = end if end <= boundary[1] else boundary[1]
        if start >= end:
            return ''

        return self.hdp.fetch_seq(var.ac, start, end)

    def _get_ref_alt(self, var, boundary):
        """Get reference allele and alternative allele of the variant
        """

        #Get reference allele
        if var.posedit.edit.type == 'ins':
            ref = ''
        elif var.posedit.edit.type == 'dup':
            if var.posedit.edit.seq:
                ref = self._fetch_bounded_seq(var, var.posedit.pos.start.base - 1, var.posedit.pos.end.base, boundary)
                #validate whether the ref of the var is the same as the reference sequence
                if var.posedit.edit.seq != ref:
                    raise HGVSValidationError(str(var) + ': ' + hgvs.validator.SEQ_ERROR_MSG)
            ref = ''
        else:
            ref = self._fetch_bounded_seq(var, var.posedit.pos.start.base - 1, var.posedit.pos.end.base, boundary)
            #validate whether the ref of the var is the same as the reference sequence
            if var.posedit.edit.ref_s is not None and var.posedit.edit.ref != '' and var.posedit.edit.ref != ref:
                raise HGVSValidationError(str(var) + ': ' + hgvs.validator.SEQ_ERROR_MSG)

        #Get alternative allele
        if var.posedit.edit.type == 'sub' or var.posedit.edit.type == 'delins' or var.posedit.edit.type == 'ins':
            alt = var.posedit.edit.alt
        elif var.posedit.edit.type == 'del':
            alt = ''
        elif var.posedit.edit.type == 'dup':
            alt = var.posedit.edit.seq or self._fetch_bounded_seq(var, var.posedit.pos.start.base - 1,
                                                                  var.posedit.pos.end.base, boundary)
        elif var.posedit.edit.type == 'inv':
            alt = ref[::-1]
        elif var.posedit.edit.type == 'identity':
            alt = ref

        return ref, alt

    def _normalize_alleles(self, var, boundary):
        """Normalize the variant until it could not be shuffled
        """

        ref, alt = self._get_ref_alt(var, boundary)
        win_size = max(len(ref), len(alt)) * 3

        if self.direction == 3:
            if var.posedit.edit.type == 'ins':
                base = var.posedit.pos.start.base
                start = 1
                stop = 1
            elif var.posedit.edit.type == 'dup':
                base = var.posedit.pos.end.base
                start = 1
                stop = 1
            else:
                base = var.posedit.pos.start.base
                start = 0
                stop = var.posedit.pos.end.base - base + 1

            while True:
                ref_seq = self._fetch_bounded_seq(var, base - 1, base - 1 + win_size, boundary)
                if ref_seq == '':
                    break
                orig_start, orig_stop = start, stop
                start, stop, (ref, alt) = normalize_alleles(ref_seq.encode('ascii'), start, stop,
                                                            (ref.encode('ascii'),
                                                             alt.encode('ascii')), len(ref_seq), win_size, False)
                if stop < len(ref_seq) or start == orig_start:
                    break
                #if stop at the end of the window, try to extend the shuffling to the right
                base += start - orig_start
                stop -= start - orig_start
                start = orig_start

        elif self.direction == 5:
            if var.posedit.edit.type == 'ins':
                base = max(var.posedit.pos.end.base - win_size + 1, boundary[0] + 1)
                start = var.posedit.pos.end.base - base
                stop = var.posedit.pos.end.base - base
            elif var.posedit.edit.type == 'dup':
                base = max(var.posedit.pos.end.base - win_size + 1, boundary[0] + 1)
                start = var.posedit.pos.end.base - base + 1
                stop = var.posedit.pos.end.base - base + 1
            else:
                base = max(var.posedit.pos.end.base - win_size + 1, boundary[0] + 1)
                start = var.posedit.pos.start.base - base
                stop = var.posedit.pos.end.base - base + 1

            while True:
                ref_seq = self._fetch_bounded_seq(var, base - 1, base - 1 + win_size, boundary)
                if ref_seq == '':
                    break
                orig_start, orig_stop = start, stop
                start, stop, (ref, alt) = normalize_alleles(ref_seq.encode('ascii'), start, stop,
                                                            (ref.encode('ascii'),
                                                             alt.encode('ascii')), 0, win_size, True)
                if start > 0 or stop == orig_stop:
                    break
                #if stop at the end of the window, try to extend the shuffling to the left
                base -= orig_stop - stop
                start += orig_stop - stop
                stop = orig_stop

        return base + start, base + stop, (ref, alt)

    def normalize(self, var):
        """Perform variants normalization
        """
        assert isinstance(var, hgvs.variant.SequenceVariant), 'variant must be a parsed HGVS sequence variant object'

        if var.posedit.uncertain:
            return var

        type = var.type

        if type == 'p':
            raise HGVSUnspportedOperationError("Unsupported normalization of protein level variants")
        if isinstance(
            var.posedit.pos.start,
            hgvs.location.BaseOffsetPosition) and var.posedit.pos.start.base != 0 and var.posedit.pos.start.offset != 0:
            raise HGVSUnspportedOperationError(
                "Unsupported normalization of intron variants at CDS and transcript level")
        if isinstance(
            var.posedit.pos.end,
            hgvs.location.BaseOffsetPosition) and var.posedit.pos.end.base != 0 and var.posedit.pos.end.offset != 0:
            raise HGVSUnspportedOperationError(
                "Unsupported normalization of intron variants at CDS and transcript level")

        # For c. variants normalization, first convert to r. variants
        # and perform normalization at the r. level then convert the
        # normalized r. variant back to c. variant at the end.
        if type == 'c':
            var = self.hm.c_to_n(var)

        bound_s, bound_e = self._get_boundary(var)
        boundary = (bound_s, bound_e)
        start, end, (ref, alt) = self._normalize_alleles(var, boundary)

        ref_len = len(ref)
        alt_len = len(alt)

        # Generate normalized variant
        if alt_len == ref_len:
            ref_start = start
            ref_end = end - 1
            #substitution
            if start == end - 1:
                edit = hgvs.edit.NARefAlt(ref=ref, alt=alt)
            #delins
            else:
                if self.fill:
                    edit = hgvs.edit.NARefAlt(ref=ref, alt=alt)
                else:
                    edit = hgvs.edit.NARefAlt(ref='', alt=alt)
        elif alt_len < ref_len:
            ref_start = start
            ref_end = end - 1
            #del
            if alt_len == 0:
                if self.fill:
                    edit = hgvs.edit.NARefAlt(ref=ref, alt=None)
                else:
                    edit = hgvs.edit.NARefAlt(ref='', alt=None)
            #delins
            else:
                if self.fill:
                    edit = hgvs.edit.NARefAlt(ref=ref, alt=alt)
                else:
                    edit = hgvs.edit.NARefAlt(ref='', alt=alt)
        elif alt_len > ref_len:
            #ins or dup
            if ref_len == 0:
                left_seq = self._fetch_bounded_seq(var, start - alt_len - 1, end - 1,
                                                   boundary) if self.direction == 3 else ''
                right_seq = self._fetch_bounded_seq(var, start - 1, start + alt_len - 1,
                                                    boundary) if self.direction == 5 else ''
                #dup
                if alt == left_seq:
                    ref_start = start - alt_len
                    ref_end = end - 1
                    if self.fill:
                        edit = hgvs.edit.Dup(seq=alt)
                    else:
                        edit = hgvs.edit.Dup(seq='')
                elif alt == right_seq:
                    ref_start = start
                    ref_end = start + alt_len - 1
                    if self.fill:
                        edit = hgvs.edit.Dup(seq=alt)
                    else:
                        edit = hgvs.edit.Dup(seq='')
                #ins
                else:
                    ref_start = start - 1
                    ref_end = end
                    edit = hgvs.edit.NARefAlt(ref=None, alt=alt)
            #delins
            else:
                ref_start = start
                ref_end = end - 1
                if self.fill:
                    edit = hgvs.edit.NARefAlt(ref=ref, alt=alt)
                else:
                    edit = hgvs.edit.NARefAlt(ref='', alt=alt)

        var_norm = copy.deepcopy(var)
        var_norm.posedit.edit = edit
        var_norm.posedit.pos.start.base = ref_start
        var_norm.posedit.pos.end.base = ref_end

        if type == 'c':
            var_norm = self.hm.n_to_c(var_norm)

        return var_norm


if __name__ == '__main__':
    hgvsparser = hgvs.parser.Parser()
    var = hgvsparser.parse_hgvs_variant('NM_001166478.1:c.61delG')
    hdp = hgvs.dataproviders.uta.connect()
    norm = Normalizer(hdp, direction=5, cross=False)
    res = norm.normalize(var)
    print(str(var) + '    =>    ' + str(res))

## <LICENSE>
## Copyright 2015 HGVS Contributors (https://bitbucket.org/biocommons/hgvs)
##
## Licensed under the Apache License, Version 2.0 (the "License");
## you may not use this file except in compliance with the License.
## You may obtain a copy of the License at
##
##     http://www.apache.org/licenses/LICENSE-2.0
##
## Unless required by applicable law or agreed to in writing, software
## distributed under the License is distributed on an "AS IS" BASIS,
## WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
## See the License for the specific language governing permissions and
## limitations under the License.
## </LICENSE>
