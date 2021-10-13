import logging
import shutil
import copy
import pandas as pd
from pathlib import Path
import os
from contextlib import contextmanager
import numpy as np
import scipy.stats as stats
import pysnptools.util as pstutil
from bgen_reader._multimemmap import MultiMemMap
from pysnptools.standardizer import Unit
from pysnptools.snpreader import SnpData
from pysnptools.pstreader import PstData
from pysnptools.snpreader import _MergeSIDs
from pysnptools.eigenreader import EigenData
from pysnptools.eigenreader.eigenreader import RotationMemMap
from pysnptools.util.mapreduce1 import map_reduce
from fastlmm.inference.fastlmm_predictor import (
    _pheno_fixup,
    _snps_fixup,
    _kernel_fixup,
)
from fastlmm.util.mingrid import minimize1D
from fastlmm.util.pickle_io import load, save

# Features could add if there is interest
#   * eigenvalues and vectors in any reasonable format
#   * snp interaction ???
#   * filter output rows to keep low p-values and a random sample
#   * Allow pre-rotated phenotype and/or covariate data (order of eigens will need to match K0_eigen
#   * Directions for a run on cluster
#   * (perhaps) Work with GPUs


# !!!LATER add warning here (and elsewhere) K0 or K1.sid_count < test_snps.sid_count,
#  might be a covar mix up.(but only if a SnpKernel
# cmk0 be good at doing one K0_eigen, too
def single_snp_eigen(
    test_snps,
    pheno,
    K0_eigen_by_chrom,
    covar=None,  # !!!cmk covar_by_chrom=None,
    leave_out_one_chrom=True,
    output_file_name=None,
    log_delta=None,
    cache_folder=None,
    stop_early=None,
    find_delta_via_reml=True,
    test_via_reml=False,
    count_A1=None,
    batch_size=None,
    runner=None,
):
    """cmk documentation"""
    # !!!LATER raise error if covar has NaN
    if output_file_name is not None:
        os.makedirs(Path(output_file_name).parent, exist_ok=True)

    cache_version = 4

    cache_folder = create_cache_folder(cache_folder, cache_version)

    # =========================
    # Figure out the data format for every input
    # =========================
    test_snps = _snps_fixup(test_snps, count_A1=count_A1)
    pheno = _pheno_fixup_and_check_missing(pheno, count_A1)
    covar = _pheno_fixup(covar, iid_if_none=pheno.iid, count_A1=count_A1)

    # =========================
    # Create a covar reader with bias column
    # (but don't read from it, yet)
    # =========================
    covar = _append_bias(covar)

    # =========================
    # Intersect and order individuals.
    # Make sure every K0_eigen has the same individuals.
    # Also that these individuals have data in
    # test_snps, pheno, and covar.
    # =========================
    chrom_list_K0_eigen = list(K0_eigen_by_chrom.keys())
    chrom_set_test_snps = set(test_snps.pos[:, 0])
    assert chrom_set_test_snps.issubset(
        chrom_list_K0_eigen
    ), "Every chromosome in test_snps but have a K0_eigen"
    chrom_list = list(chrom_set_test_snps)
    K0_eigen_list = list(K0_eigen_by_chrom.values())
    assert len(K0_eigen_list) > 0, "Expect at least one K0_eigen"
    iid_count_before = K0_eigen_list[0].row_count
    assert np.all(
        [K0_eigen.row_count == iid_count_before for K0_eigen in K0_eigen_list]
    ), "Every K0_eigen must have the same number of individuals"
    #!!!cmk kludge is it OK if some K0_eigens are repeats and they get reordered?
    intersected = pstutil.intersect_apply([test_snps, pheno, covar] + K0_eigen_list)
    test_snps, pheno, covar = intersected[0:3]
    K0_eigen_list = intersected[3:]
    assert np.all(
        [K0_eigen.row_count == iid_count_before for K0_eigen in K0_eigen_list]
    ), "Must have test_snps, pheno, and covar data for each K0_eigen individual"

    K0_eigen_by_chrom = {
        chrom: K0_eigen for chrom, K0_eigen in zip(chrom_list_K0_eigen, K0_eigen_list)
    }

    # !!!cmk assert covar_by_chrom is None, "When 'leave_out_one_chrom' is False,
    #  'covar_by_chrom' must be None"
    # !!!cmk K0, K1, block_size = _set_block_size(K0, K1, mixing, GB_goal,

    # !!! cmk
    # if h2 is not None and not isinstance(h2, np.ndarray):
    #     h2 = np.repeat(h2, pheno.shape[1])

    if stop_early == 0:
        return

    #!!!cmk1 cache this
    # ===============================
    # If needed later for REML, compute covarTcovar
    # and logdet(eigen(covarTcovar))
    # ===============================
    if test_via_reml or find_delta_via_reml:
        covarTcovar, logdet_covarTcovar = _find_covarTcovar_etc(covar, cache_folder)
    else:
        covarTcovar = None
        logdet_covarTcovar = None

    if stop_early == 1:
        return

    #!!!cmk1 cache this
    # ===============================
    # In parallel, for each chrom,
    # rotate the covariates and phenotypes
    # ===============================
    per_chrom_list = _find_per_chrom_list(
        chrom_list,
        K0_eigen_by_chrom,
        covar,
        pheno,
        batch_size,
        cache_folder,
        runner,
    )

    if stop_early == 2:
        return

    # ===============================
    # In parallel, for each chrom & each pheno
    # find the best h2 and related info
    # ===============================
    per_pheno_per_chrom_list = _find_per_pheno_per_chrom_list(
        logdet_covarTcovar,
        per_chrom_list,
        K0_eigen_by_chrom,
        pheno,
        find_delta_via_reml,
        test_via_reml,
        log_delta,
        batch_size,
        cache_folder,
        runner,
    )

    #!!!cmk give an error mesage if out of range
    if stop_early == 3:
        return

    # ==================================
    # Test SNPs in batches
    # ==================================
    dataframe = _test_in_batches(
        covar,
        covarTcovar,
        per_chrom_list,
        per_pheno_per_chrom_list,
        test_snps,
        K0_eigen_by_chrom,
        test_via_reml,
        batch_size,
        runner,
    )

    if output_file_name is not None:  #!!!cmk test
        dataframe.to_csv(output_file_name, sep="\t", index=False)

    return dataframe


def _pheno_fixup_and_check_missing(pheno, count_A1):
    # We read pheno here and again in _find_per_pheno_per_chrom_list
    # because they might be running in different processes and
    # re-reading isn't expensive.
    pheno = _pheno_fixup(pheno, count_A1=count_A1).read()
    good_values_per_iid = (pheno.val == pheno.val).sum(axis=1)
    assert not np.any(
        (good_values_per_iid > 0) * (good_values_per_iid < pheno.sid_count)
    ), "With multiple phenotypes, an individual's values must either be all missing or have no missing."
    # !!!cmk multipheno
    # drop individuals with no good pheno values.
    pheno = pheno[good_values_per_iid > 0, :]

    assert pheno.sid_count >= 1, "Expect at least one phenotype"
    # !!!cmk assert pheno.sid_count == 1, "currently only have code for one pheno"

    return pheno


def _append_bias(covar):
    # !!!cmk what is "bias' is already used as column name
    bias = SnpData(iid=covar.iid, sid=["bias"], val=np.ones((covar.iid_count, 1)))
    covar_and_bias = _MergeSIDs([covar, bias])
    return covar_and_bias


# !!!cmk needs better name
class KdI:
    def __init__(self, hld, row, is_low_rank, logdet, Sd):
        self.h2, self.log_delta, self.delta = hld
        assert len(Sd.shape) == 1, "Expect Sd to be a 1-D array"

        self.row = row
        self.is_low_rank = is_low_rank
        self.logdet = logdet
        self.Sd = Sd

    @staticmethod
    def from_eigendata(eigendata, h2=None, log_delta=None, delta=None):
        hld = KdI._hld(h2, log_delta, delta)
        _, _, delta = hld

        logdet, Sd = eigendata.logdet(delta)

        return KdI(
            hld,
            row=eigendata.row,
            is_low_rank=eigendata.is_low_rank,
            logdet=logdet,
            Sd=Sd,
        )

    @staticmethod
    def _hld(h2=None, log_delta=None, delta=None):
        assert (
            sum([h2 is not None, log_delta is not None, delta is not None]) == 1
        ), "Exactly one of h2, etc should have a value"
        if h2 is not None:
            delta = 1.0 / h2 - 1.0
            log_delta = np.log(delta)
        elif log_delta is not None:
            log_delta = log_delta
            delta = np.exp(log_delta)
            h2 = 1.0 / (delta + 1)
        elif delta is not None:  #!!!cmk test
            delta = delta
            log_delta = np.log(delta) if delta != 0 else None
            h2 = 1.0 / (delta + 1)
        else:
            assert False, "real assert"
        return h2, log_delta, delta

    @property
    def row_count(self):
        return len(self.row)


# !!!cmk move to PySnpTools
class AKB(PstData):
    def __init__(self, val, row, col, kdi):
        super().__init__(val=val, row=row, col=col)
        self.kdi = kdi

    @staticmethod
    def from_rotations(a_r, kdi, b_r, aK=None):
        if aK is None:
            aK = a_r.val / kdi.Sd[:, np.newaxis]

        val = aK.T @ b_r.val

        if kdi.is_low_rank:
            val += a_r.double.val.T @ b_r.double.val / kdi.delta

        result = AKB(val=val, row=a_r.col, col=b_r.col, kdi=kdi)
        return result, aK

    @staticmethod
    def empty(row, col, kdi):
        return AKB(
            val=np.full(shape=(len(row), len(col)), fill_value=np.NaN),
            row=row,
            col=col,
            kdi=kdi,
        )

    def __setitem__(self, key, value):
        self.val[key] = value.val

    def __getitem__(self, index):
        val = self.val[index]
        return AKB(
            val=val,
            row=self.row[index[0]],
            col=self.col[index[1]],
            kdi=self.kdi,
        )

    @property
    def T(self):
        return AKB(
            val=np.moveaxis(self.val, 0, 1), row=self.col, col=self.row, kdi=self.kdi
        )


# We pass K0_eigen, but only use metadata such as the eigenvalues
def _find_h2(
    K0_eigen,
    logdet_xtx,
    X_row_count,
    X_r,
    pheno_r,
    use_reml,
    nGridH2=10,
    minH2=0.0,
    maxH2=0.99999,
):
    # !!!cmk log delta is used here. Might be better to use findH2, but if so will need to normalized G so that its kdi's diagonal would sum to iid_count
    logging.info("searching for delta/h2/logdelta")

    resmin = [None]

    def f(x, resmin=resmin, **kwargs):
        kdi = KdI.from_eigendata(K0_eigen, h2=x)
        phenoKpheno, _ = AKB.from_rotations(pheno_r, kdi, pheno_r)
        XKX, XK = AKB.from_rotations(X_r, kdi, X_r)
        XKpheno, _ = AKB.from_rotations(X_r, kdi, pheno_r, aK=XK)

        nLL, _, _ = _loglikelihood(
            logdet_xtx, X_row_count, phenoKpheno, XKX, XKpheno, use_reml=use_reml
        )
        nLL = -nLL  # !!!cmk
        if (resmin[0] is None) or (nLL < resmin[0]["nLL"]):
            resmin[0] = {"nLL": nLL, "h2": x}
        logging.debug(f"search\t{x}\t{nLL}")
        return nLL

    _ = minimize1D(f=f, nGrid=nGridH2, minval=0.00001, maxval=maxH2)
    return resmin[0]


def _find_beta(yKy, XKX, XKy):

    ###############################################################
    # BETA
    #
    # ref: https://math.unm.edu/~james/w15-STAT576b.pdf
    # You can minimize squared error in linear regression with a beta of
    # beta = np.linalg.inv(XTX) @ X.T @ y
    #  where XTX = X.T @ X #!!!cmk kludge give reference for XKX, too
    #
    # ref: https://en.wikipedia.org/wiki/Eigendecomposition_of_a_matrix#Matrix_inverse_via_eigendecomposition
    # You can find an inverse of XTX using eigen
    # print(np.linalg.inv(XTX))
    # values,vectors = np.linalg.eigh(XTX)
    # print((vectors/values) @ vectors.T)
    #
    # So, beta = (vectors/values) @ vectors.T @ X.T @ y
    # or  beta = vectors @ (vectors.T @ (X.T @ y)/values)

    eigen_xkx = EigenData.from_aka(XKX, keep_above=1e-10)
    #!!!cmk why two different ways to talk about chking low rank?
    XKy_r_s = eigen_xkx.rotate_and_scale(XKy, ignore_low_rank=True)
    beta = eigen_xkx.rotate_back(XKy_r_s, check_low_rank=False)

    ##################################################################
    # residual sum of squares, RSS (aka SSR aka SSE)
    #
    # ref 1: https://en.wikipedia.org/wiki/Residual_sum_of_squares#Matrix_expression_for_the_OLS_residual_sum_of_squares
    # ref 2: http://www.web.stanford.edu/~mrosenfe/soc_meth_proj3/matrix_OLS_NYU_notes.pdf
    # RSS = ((y-y_predicted)**2).sum()
    # RSS = ((y-y_predicted).T @ (y-y_predicted))
    # RSS = (y - X @ beta).T @ (y - X @ beta)
    # recall that (a-b).T @ (a-b) = a.T@a - 2*a.T@b + b.T@b
    # RSS = y.T @ y - 2*y.T @ (X @ beta) + X @ beta.T @ (X @ beta)
    # ref2: beta is choosen s.t. y.T@X = X@beta.T@X aka X.T@X@beta
    # RSS = y.T @ y - 2*y.T @ (X @ beta) + y.T @ X @ beta)
    # RSS = y.T @ y - y.T @ X @ beta

    rss = float(yKy.val - XKy.val.T @ beta.val)

    return eigen_xkx, beta, rss


def _loglikelihood(logdet_xtx, X_row_count, yKy, XKX, XKy, use_reml):
    if use_reml:
        nLL, beta = _loglikelihood_reml(logdet_xtx, X_row_count, yKy, XKX, XKy)
        return nLL, beta, None
    else:
        return _loglikelihood_ml(yKy, XKX, XKy)


# Note we have both XKX with XTX
def _loglikelihood_reml(logdet_xtx, X_row_count, yKy, XKX, XKy):
    kdi = yKy.kdi

    eigen_xkx, beta, rss = _find_beta(yKy, XKX, XKy)
    logdet_xkx, _ = eigen_xkx.logdet()

    X_row_less_col = X_row_count - XKX.shape[0]

    sigma2 = rss / X_row_less_col
    nLL = 0.5 * (
        kdi.logdet
        + logdet_xkx
        - logdet_xtx
        + X_row_less_col * (np.log(2.0 * np.pi * sigma2) + 1)
    )

    assert np.isreal(
        nLL
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta


def _loglikelihood_ml(yKy, XKX, XKy):
    kdi = yKy.kdi

    eigen_xkx, beta, rss = _find_beta(yKy, XKX, XKy)

    sigma2 = rss / kdi.row_count
    nLL = 0.5 * (kdi.logdet + kdi.row_count * (np.log(2.0 * np.pi * sigma2) + 1))
    assert np.isreal(
        nLL
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # This is a faster version of h2 * sigma2 * np.diag(LA.inv(XKX))
    # where h2*sigma2 is sigma2_g
    #!!!cmk kludge need to test these
    variance_beta = (
        kdi.h2
        * sigma2
        * (eigen_xkx.vectors / eigen_xkx.values * eigen_xkx.vectors).sum(-1)
    )
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta, variance_beta


# Returns a kdi that is the original Kg + delta I
# (We pass K0_eigen, but only use metadata such as eigenvalues)
def _find_best_kdi_as_needed(
    K0_eigen, logdet_covarTcovar, covar_r, pheno_r, use_reml, log_delta=None
):
    if log_delta is None:
        # cmk As per the paper, we optimized delta with use_reml=True, but
        # cmk we will later optimize beta and find log likelihood with ML (use_reml=False)
        h2 = _find_h2(
            K0_eigen,
            logdet_covarTcovar,
            K0_eigen.row_count,
            covar_r,
            pheno_r,
            use_reml=use_reml,
            minH2=0.00001,
        )["h2"]
        return KdI.from_eigendata(K0_eigen, h2=h2)
    else:
        # !!!cmk internal/external doesn't matter if full rank, right???
        return KdI.from_eigendata(K0_eigen, log_delta=log_delta)


# !!!cmk similar to single_snp.py and single_snp_scale
def _create_dataframe():
    # https://stackoverflow.com/questions/21197774/assign-pandas-dataframe-column-dtypes
    dataframe = pd.DataFrame(
        np.empty(
            (0,),
            dtype=[
                ("sid_index", np.float),
                ("SNP", "S"),
                ("Chr", np.float),
                ("GenDist", np.float),
                ("ChrPos", np.float),
                ("PValue", np.float),
                ("SnpWeight", np.float),
                ("SnpWeightSE", np.float),
                ("SnpFractVarExpl", np.float),
                ("Mixing", np.float),
                ("Nullh2", np.float),
            ],
        )
    )
    return dataframe


#!!!cmk where should this live?
def eigen_from_kernel(K0, kernel_standardizer, count_A1=None):
    """!!!cmk documentation"""
    # !!!cmk could offer a low-memory path that uses memmapped files
    from pysnptools.kernelreader import SnpKernel
    from pysnptools.kernelstandardizer import Identity as KS_Identity

    assert K0 is not None
    K0 = _kernel_fixup(K0, iid_if_none=None, standardizer=Unit(), count_A1=count_A1)
    assert K0.iid0 is K0.iid1, "Expect K0 to be square"

    if isinstance(
        K0, SnpKernel
    ):  # !!!make eigen creation a method on all kernel readers
        assert isinstance(
            kernel_standardizer, KS_Identity
        ), "cmk need code for other kernel standardizers"
        vectors, sqrt_values, _ = np.linalg.svd(
            K0.snpreader.read().standardize(K0.standardizer).val,
            full_matrices=False,
        )
        if np.any(sqrt_values < -0.1):
            logging.warning("kernel contains a negative Eigenvalue")
        eigen = EigenData(values=sqrt_values * sqrt_values, vectors=vectors, iid=K0.iid)
    else:
        # !!!cmk understand _read_kernel, _read_with_standardizing
        #!!!cmk test
        K0 = K0._read_with_standardizing(
            kernel_standardizer=kernel_standardizer,
            to_kerneldata=True,
            return_trained=False,
        )
        # !!! cmk ??? pass in a new argument, the kernel_standardizer(???)
        logging.debug("About to eigh")
        w, v = np.linalg.eigh(K0.val)  # !!! cmk do SVD sometimes?
        logging.debug("Done with to eigh")
        if np.any(w < -0.1):
            logging.warning(
                "kernel contains a negative Eigenvalue"
            )  # !!!cmk this shouldn't happen with a RRM, right?
        # !!!cmk remove very small eigenvalues
        # !!!cmk remove very small eigenvalues in a way that doesn't require a memcopy?
        eigen = EigenData(values=w, vectors=v, iid=K0.iid)
        # eigen.vectors[:,eigen.values<.0001]=0.0
        # eigen.values[eigen.values<.0001]=0.0
        # eigen = eigen[:,eigen.values >= .0001] # !!!cmk const
    return eigen


#!!!cmk kludge - reorder inputs
def _find_per_chrom_list(
    chrom_list,
    K0_eigen_by_chrom,
    covar,
    pheno,
    batch_size,
    cache_folder,
    runner,
):
    # =========================
    # Unless the cache is complete,
    # read covar and pheno into memory
    # so that we can rotate them by each
    # chromosome's eigen.
    # =========================
    cache_folder1 = create_cache_subfolder(cache_folder, "step2.per_chrom_list")
    if not cache_is_complete(cache_folder1):
        covar = covar.read(view_ok=True)
        pheno = pheno.read(view_ok=True)
    else:
        covar = None
        pheno = None

    # for each chrom (in parallel):
    def mapper_find_per_pheno_list(chrom):
        # =========================
        # Find the K0_eigen for this chrom.
        # Next, read it in batches and
        # rotate covar and pheno.
        #
        # An "EigenReader" object includes both the vectors and values.
        # A RotationReader object always includes both the main "rotated" array.
        # In addition, if eigen was low rank, then also a "double" array
        # [such that double = input-eigenvectors@rotated]
        # that captures information lost by the low rank.
        # =========================

        cache_folder2 = create_cache_subfolder(cache_folder1, f"chrom{chrom}")
        if cache_is_complete(cache_folder2):
            return {
                "chrom": int(chrom),
                "covar_r": RotationMemMap(cache_folder2 / "covar_r.{0}.memmap"),
                "pheno_r": RotationMemMap(cache_folder2 / "pheno_r.{0}.memmap"),
            }

        K0_eigen = K0_eigen_by_chrom[chrom]
        logging.info("rotating covar and pheno")
        covar_r, pheno_r = K0_eigen.rotate_list([covar, pheno], batch_rows=batch_size)

        if cache_folder is not None:
            empty_cache(cache_folder2)
            covar_r = RotationMemMap.write(
                cache_folder2 / "covar_r.{0}.memmap", covar_r
            )
            pheno_r = RotationMemMap.write(
                cache_folder2 / "pheno_r.{0}.memmap", pheno_r
            )
            mark_cache_complete(cache_folder2)

        return {
            "chrom": int(chrom),
            "covar_r": covar_r,
            "pheno_r": pheno_r,
        }

    per_chrom_list = map_reduce(
        chrom_list,
        mapper=mapper_find_per_pheno_list,
        runner=runner,
    )

    mark_cache_complete(cache_folder1)

    return per_chrom_list


#!!!cmk kludge - reorder inputs
def _find_per_pheno_per_chrom_list(
    logdet_covarTcovar,
    per_chrom_list,
    K0_eigen_by_chrom,
    pheno,
    find_delta_via_reml,
    test_via_reml,
    log_delta,
    batch_size,
    cache_folder,
    runner,
):

    # =========================
    # Unless the cache is complete,
    # read pheno into memory.
    # This avoids reading from disk for each chrom x pheno
    # =========================
    cache_folder1 = create_cache_subfolder(
        cache_folder, "step3.per_pheno_per_chrom_list"
    )
    if cache_is_complete(cache_folder1):
        # Make this "do nothing" explicit for coverage testing
        pheno = pheno
    else:
        pheno = pheno.read(view_ok=True, order="A")

    # for each chrom (in parallel):
    def mapper_find_per_pheno_list(per_chrom):
        chrom = per_chrom["chrom"]
        cache_folder2 = create_cache_subfolder(cache_folder1, f"chrom{chrom}")
        # !!!cmk do view_ok's also need order="A"?

        # !!!cmk comments
        if cache_is_complete(cache_folder2):
            covar_r = None
            pheno_r = None
            K0_eigen = None
            cc, x_sid = None, None
        else:
            covar_r = per_chrom["covar_r"].read(view_ok=True)
            pheno_r = per_chrom["pheno_r"].read(view_ok=True)
            # =========================
            # Read K0_eigen for this chrom into memory.
            #
            # An "EigenReader" object includes both the vectors and values.
            # A RotationReader object always includes both the main "rotated" array.
            # In addition, if eigen was low rank, then also a "double" array
            # [such that double = input-eigenvectors@rotated]
            # that captures information lost by the low rank.
            # =========================
            K0_eigen = K0_eigen_by_chrom[chrom]

            # ========================================
            # cc is the covariate count.
            # x_sid is the names of the covariates plus "alt"
            # ========================================
            cc, x_sid = _cc_and_x_sid(covar_r)

        # =========================
        # For each phenotype, in parallel, ...
        # Find the K0+delta I with the best likelihood.
        # A KdI object includes
        #   * Sd = eigenvalues + delta
        #   * is_low_rank (True/False)
        #   * logdet
        #
        # Next, find A^T * K^-1 * B for covar and pheno.
        # "AKB.from_rotations" works for both full and low-rank.
        # A AKB object includes
        #   * The AKB value
        #   * The KdI objected use to create it.
        #
        # Finally, find null likelihood.
        # =========================

        # for each pheno (in parallel):
        def mapper_search(pheno_index):
            cache_folder3 = create_cache_subfolder(cache_folder2, f"pheno{pheno_index}")
            if cache_is_complete(cache_folder3):
                return PerPhenoReader(cache_folder3)

            per_pheno_data = PerPhenoData()

            pheno_r_i = pheno_r[pheno_index]
            per_pheno_data.K0_kdi = _find_best_kdi_as_needed(
                K0_eigen,
                logdet_covarTcovar,
                covar_r,
                pheno_r_i,
                use_reml=find_delta_via_reml,
                log_delta=log_delta,
            )
            covarKcovar, per_pheno_data.covarK = AKB.from_rotations(
                covar_r, per_pheno_data.K0_kdi, covar_r
            )
            per_pheno_data.phenoKpheno, _ = AKB.from_rotations(
                pheno_r_i, per_pheno_data.K0_kdi, pheno_r_i
            )
            covarKpheno, _ = AKB.from_rotations(
                covar_r, per_pheno_data.K0_kdi, pheno_r_i, aK=per_pheno_data.covarK
            )
            per_pheno_data.ll_null, _beta, _variance_beta = _loglikelihood(
                logdet_covarTcovar,
                K0_eigen.row_count,
                per_pheno_data.phenoKpheno,
                covarKcovar,
                covarKpheno,
                use_reml=test_via_reml,
            )

            # ==================================
            # Recall that X is the covariates (with bias) and one test SNP.
            # Create an XKX, and XKpheno where
            # the last part can be swapped for each test SNP.
            # ==================================
            per_pheno_data.XKX = AKB.empty(
                row=x_sid, col=x_sid, kdi=per_pheno_data.K0_kdi
            )
            per_pheno_data.XKX[:cc, :cc] = covarKcovar  # upper left

            per_pheno_data.XKpheno = AKB.empty(
                x_sid, pheno_r_i.col, kdi=per_pheno_data.K0_kdi
            )
            per_pheno_data.XKpheno[:cc, :] = covarKpheno  # upper

            if cache_folder is not None:
                empty_cache(cache_folder3)
                per_pheno_reader = PerPhenoReader.write(cache_folder3, per_pheno_data)
                mark_cache_complete(cache_folder3)
            else:
                per_pheno_reader = per_pheno_data

            return per_pheno_reader

        def reducer_search(per_pheno_sequence):
            per_pheno_list = list(per_pheno_sequence)
            mark_cache_complete(cache_folder2)
            return per_pheno_list

        return map_reduce(
            range(pheno.col_count), mapper=mapper_search, reducer=reducer_search
        )

    #!!!cmk kludge be consistent with if "reducer", "mapper", "eigen" go at front or back of variable
    per_pheno_per_chrom_list = map_reduce(
        per_chrom_list,
        nested=mapper_find_per_pheno_list,
        runner=runner,
    )

    mark_cache_complete(cache_folder1)
    return per_pheno_per_chrom_list


# !!!cmk what if "alt" name is taken?
def _cc_and_x_sid(covar):
    cc = covar.col_count  # covariate count (including bias)
    x_sid = np.append(covar.col, "alt")
    return cc, x_sid


#!!!cmk reorder inputs kludge
def _test_in_batches(
    covar,
    covarTcovar,
    per_chrom_list,
    per_pheno_per_chrom_list,
    test_snps,
    K0_eigen_by_chrom,
    test_via_reml,
    batch_size,
    runner,
):

    # ==================================
    # X is the covariates plus one test SNP called "alt"
    # Only need in-memory covar and XTX for REML.
    # cc is covariate count (not including "alt")
    cc, x_sid = _cc_and_x_sid(covar)
    if test_via_reml:
        covar = covar.read(view_ok=True)
        XTX = PstData(
            val=np.full((cc + 1, cc + 1), fill_value=np.nan),
            row=x_sid,
            col=x_sid,
        )
        XTX.val[:cc, :cc] = covarTcovar.val
    else:
        XTX = None

    # for each chrom (in parallel):
    def df_per_chrom_mapper(chrom_index):
        # ===========================
        # Look up the pre-computed info for this chromosome.
        # ===========================
        per_chrom = per_chrom_list[chrom_index]
        per_pheno_list = per_pheno_per_chrom_list[chrom_index]
        chrom = per_chrom["chrom"]
        #!!!cmk similar code elsewhere
        covar_r = per_chrom["covar_r"].read(view_ok=True)
        pheno_r = per_chrom["pheno_r"].read(view_ok=True)
        pheno_count = len(per_pheno_list)

        # ==============================
        # Find the EigenReader for this chrom,
        # but don't read, yet.
        # ==============================
        K0_eigen = K0_eigen_by_chrom[chrom]

        # =========================
        # Create a testsnp reader for this chrom, but don't read yet.
        # =========================
        test_snps_for_chrom = test_snps[:, test_snps.pos[:, 0] == chrom]
        batch_size_test_snps = (
            batch_size if batch_size is not None else test_snps_for_chrom.sid_count + 1
        )

        # For each test_snp batch (in parallel) ...
        def mapper(sid_start):
            logging.info(f"sid_start={sid_start:,d} of {test_snps_for_chrom.sid_count:,d} by {batch_size_test_snps:,d}")
            # ==================================
            # Read and standardize a batch of test SNPs. Then rotate.
            # Then, for each pheno ...
            #   Find A^T * K^-1 * B for covar & pheno vs. the batch
            #   Find the likelihood and pvalue for each test SNP.
            # ==================================
            alt_batch = (
                test_snps_for_chrom[:, sid_start : sid_start + batch_size_test_snps]
                .read()
                .standardize()
            )
            alt_batch_r = K0_eigen.rotate(alt_batch, batch_rows=batch_size)

            # ==================================
            # For each phenotype
            # ==================================
            result_list = []
            for pheno_index, (per_pheno_reader, pheno_r_i) in enumerate(zip(per_pheno_list, pheno_r)):
                #logging.info(f"sid_start={sid_start}, pheno={pheno_index}")
                with per_pheno_reader.read() as per_pheno_data:

                    for result in _generate_results(
                        K0_eigen,
                        per_pheno_data,
                        covar,
                        covar_r,
                        alt_batch,
                        alt_batch_r,
                        pheno_r_i,
                        cc,
                        test_via_reml,
                        XTX,
                    ):
                        result_list.append(result)

            df_per_batch = _create_dataframe().append(result_list, ignore_index=True)
            df_per_batch["sid_index"] = np.repeat(
                np.arange(sid_start, sid_start + alt_batch.sid_count), pheno_count
            )
            df_per_batch["SNP"] = np.repeat(alt_batch.sid, pheno_count)
            df_per_batch["Chr"] = np.repeat(alt_batch.pos[:, 0], pheno_count)
            df_per_batch["GenDist"] = np.repeat(alt_batch.pos[:, 1], pheno_count)
            df_per_batch["ChrPos"] = np.repeat(alt_batch.pos[:, 2], pheno_count)
            # !!!cmk in lmmcov, but not lmm
            # df_per_batch['SnpFractVarExpl'] = np.sqrt(fraction_variance_explained_beta[:,0])
            # !!!cmk Feature not supported. could add "0"
            # df_per_batch['Mixing'] = np.zeros((len(sid))) + 0

            return df_per_batch

        def reducer2(df_per_batch_sequence):  #!!!cmk kludge rename
            df_per_chrom = pd.concat(df_per_batch_sequence)
            return df_per_chrom

        return map_reduce(
            range(0, test_snps_for_chrom.sid_count, batch_size_test_snps),
            mapper=mapper,  #!!!cmk kludge rename
            reducer=reducer2,
        )

    def df_per_chrom_reducer(df_per_batch_sequence):
        dataframe = pd.concat(df_per_batch_sequence)
        dataframe.sort_values(by="PValue", inplace=True)
        dataframe.index = np.arange(len(dataframe))
        return dataframe

    dataframe = map_reduce(
        range(len(per_chrom_list)),
        nested=df_per_chrom_mapper,  #!!!cmk kludge rename
        reducer=df_per_chrom_reducer,
        runner=runner,
    )

    return dataframe


def create_cache_folder(cache_folder, version):
    if cache_folder is None:
        return None
    else:
        cache_folder = Path(f"{cache_folder}.v{version}")
        cache_folder.mkdir(exist_ok=True)
        return cache_folder


def create_cache_subfolder(cache_folder, subfolder_name):
    if cache_folder is None:
        return None
    else:
        cache_subfolder = cache_folder / subfolder_name
        cache_subfolder.mkdir(exist_ok=True)
        return cache_subfolder


def cache_is_complete(cache_subfolder):
    if cache_subfolder is None:
        return False
    return (cache_subfolder / "complete.txt").exists()


def mark_cache_complete(cache_folder):
    if cache_folder is not None:
        (cache_folder / "complete.txt").touch()  #!!!cmk const


def empty_cache(cache_folder):
    if cache_folder.exists():
        shutil.rmtree(cache_folder)
    cache_folder.mkdir()


class PerPhenoData:
    def __init__(self):
        pass

    @contextmanager
    def read(self):
        yield self


class PerPhenoReader:
    def __init__(self, folder):
        self.folder = Path(folder)

    @contextmanager
    def read(self):
        per_pheno_data = load(str(self.folder / "smallstuff.pickle"))
        mmm_r = MultiMemMap(self.folder / "covarK_Sd_row.memmap", mode="r")
        try:
            per_pheno_data.covarK = mmm_r["covarK"]
            per_pheno_data.K0_kdi.Sd = mmm_r["Sd"]
            per_pheno_data.K0_kdi.row = mmm_r["row"]
            per_pheno_data.XKX.kdi = per_pheno_data.K0_kdi
            per_pheno_data.XKpheno.kdi = per_pheno_data.K0_kdi
            per_pheno_data.phenoKpheno.kdi = per_pheno_data.K0_kdi
            yield per_pheno_data
        finally:
            mmm_r.close()

    @staticmethod
    def write(folder, per_pheno_data):
        folder = Path(folder)

        # Create a shallow copy of the per_pheno_data.
        # Then pull out the big stuff and, in the copy, set big stuff to None
        # Finally save the small stuff as a pickle
        # and the big stuff as a file with multiple memory maps.
        per_pheno_data1 = copy.copy(per_pheno_data)
        covarK = per_pheno_data1.covarK
        per_pheno_data1.covarK = None
        per_pheno_data1.K0_kdi = copy.copy(per_pheno_data1.K0_kdi)
        Sd = per_pheno_data1.K0_kdi.Sd
        per_pheno_data1.K0_kdi.Sd = None
        row = per_pheno_data1.K0_kdi.row
        per_pheno_data1.K0_kdi.row = None
        per_pheno_data1.XKX = copy.copy(per_pheno_data1.XKX)
        per_pheno_data1.XKX.kdi = None
        per_pheno_data1.XKpheno = copy.copy(per_pheno_data1.XKpheno)
        per_pheno_data1.XKpheno.kdi = None
        per_pheno_data1.phenoKpheno = copy.copy(per_pheno_data1.phenoKpheno)
        per_pheno_data1.phenoKpheno.kdi = None

        save(str(folder / "smallstuff.pickle"), per_pheno_data1)
        with MultiMemMap(folder / "covarK_Sd_row.memmap", mode="w+") as mmm_wplus:
            mmm_wplus.append_empty("covarK", shape=covarK.shape, dtype=covarK.dtype)[
                ...
            ] = covarK
            mmm_wplus.append_empty("Sd", shape=Sd.shape, dtype=Sd.dtype)[...] = Sd
            mmm_wplus.append_empty("row", shape=row.shape, dtype=row.dtype)[...] = row

        # print(len(per_pheno_data.K0_kdi.Sd))
        # print(len(per_pheno_data.covarK))
        # print(len(per_pheno_data.K0_kdi.row))
        # print(len(per_pheno_data.XKpheno.kdi.Sd)) # same as above
        # print(len(per_pheno_data.phenoKpheno.kdi.Sd)) # same as above
        # filename = str(filename)
        # save(filename, per_pheno_data)
        return PerPhenoReader(folder)


def _find_covarTcovar_etc(covar, cache_folder):
    cache_prefix = "step1.covarTcovar_etc"
    cache_folder1 = create_cache_subfolder(cache_folder, cache_prefix)
    if cache_is_complete(cache_folder1):
        return load(str(cache_folder1 / (cache_prefix + ".pickle")))

    covar_data = covar.read(view_ok=True)
    covarTcovar = PstData(
        val=covar_data.val.T @ covar_data.val, row=covar.sid, col=covar.sid
    )
    eigen_covarTcovar = EigenData.from_aka(covarTcovar)
    logdet_covarTcovar, _ = eigen_covarTcovar.logdet()

    if cache_folder is not None:
        empty_cache(cache_folder1)
        save(
            str(cache_folder1 / (cache_prefix + ".pickle")),
            (covarTcovar, logdet_covarTcovar),
        )
        mark_cache_complete(cache_folder1)

    return covarTcovar, logdet_covarTcovar


def _generate_results(
    K0_eigen,
    per_pheno_data,
    covar,
    covar_r,
    alt_batch,
    alt_batch_r,
    pheno_r_i,
    cc,
    test_via_reml,
    XTX,
):

    covarKalt_batch, _ = AKB.from_rotations(
        covar_r,
        per_pheno_data.K0_kdi,
        alt_batch_r,
        aK=per_pheno_data.covarK,
    )
    alt_batchKpheno, alt_batchK = AKB.from_rotations(
        alt_batch_r, per_pheno_data.K0_kdi, pheno_r_i
    )

    # ==================================
    # For each test SNP in the batch
    # ==================================
    for i in range(alt_batch.sid_count):
        alt_r = alt_batch_r[i]

        # ==================================
        # For each pheno (as the last dimension in the matrix) ...
        # Find alt^T * K^-1 * alt for the test SNP.
        # Fill in last value of X, XKX and XKpheno
        # with the alt value.
        # ==================================
        altKalt, _ = AKB.from_rotations(
            alt_r,
            per_pheno_data.K0_kdi,
            alt_r,
            aK=alt_batchK[:, i : i + 1],
        )

        per_pheno_data.XKX[:cc, cc:] = covarKalt_batch[:, i : i + 1]  # upper right
        per_pheno_data.XKX[cc:, :cc] = per_pheno_data.XKX[:cc, cc:].T  # lower left
        per_pheno_data.XKX[cc:, cc:] = altKalt[:, :]  # lower right

        per_pheno_data.XKpheno[cc:, :] = alt_batchKpheno[i : i + 1, :]  # lower

        # Only need "logdet_xtx" for REML
        if test_via_reml:
            alt_val = alt_batch.val[:, i : i + 1]
            XTX.val[cc:, :cc] = alt_val.T @ covar.val
            XTX.val[:cc, cc:] = XTX.val[cc:, :cc].T
            XTX.val[cc:, cc:] = alt_val.T @ alt_val
            eigen_xtx = EigenData.from_aka(XTX)
            logdet_xtx, _ = eigen_xtx.logdet()
        else:
            logdet_xtx = None

        # ==================================
        # Find likelihood with test SNP and score.
        # ==================================
        ll_alt, beta, variance_beta = _loglikelihood(
            logdet_xtx,
            K0_eigen.row_count,
            per_pheno_data.phenoKpheno,
            per_pheno_data.XKX,
            per_pheno_data.XKpheno,
            use_reml=test_via_reml,
        )

        test_statistic = ll_alt - per_pheno_data.ll_null

        yield {
            "PValue": stats.chi2.sf(2.0 * test_statistic, df=1),
            "SnpWeight": beta.val,  #!!!cmk
            "SnpWeightSE": np.sqrt(variance_beta)
            if variance_beta is not None
            else None,
            # !!!cmk right name and place?
            "Pheno": pheno_r_i.col[0],
            "Nullh2": per_pheno_data.K0_kdi.h2,
        }
