import logging
import pandas as pd
import os
from pathlib import Path
import numpy as np
import scipy.stats as stats
import pysnptools.util as pstutil
from pysnptools.standardizer import Unit
from pysnptools.snpreader import SnpData
from pysnptools.pstreader import PstData
from pysnptools.eigenreader import EigenData
from fastlmm.inference.fastlmm_predictor import (
    _pheno_fixup,
    _snps_fixup,
    _kernel_fixup,
)
from fastlmm.util.mingrid import minimize1D


# !!!LATER add warning here (and elsewhere) K0 or K1.sid_count < test_snps.sid_count,
#  might be a covar mix up.(but only if a SnpKernel
def single_snp_eigen(
    test_snps,
    pheno,
    K0_eigen,
    covar=None,  # !!!cmk covar_by_chrom=None, leave_out_one_chrom=True,
    output_file_name=None,
    log_delta=None,
    # !!!cmk cache_file=None, GB_goal=None, interact_with_snp=None,
    # !!!cmk runner=None, map_reduce_outer=True,
    # !!!cmk pvalue_threshold=None,
    # !!!cmk random_threshold=None,
    # !!!cmk random_seed = 0,
    # min_log_delta=-5,  # !!!cmk make this a range???
    # max_log_delta=10,
    # !!!cmk xp=None,
    find_delta_via_reml=True,
    test_via_reml=False,
    count_A1=None,
):
    """cmk documentation"""
    # !!!LATER raise error if covar has NaN
    if output_file_name is not None:
        os.makedirs(Path(output_file_name).parent, exist_ok=True)

    # =========================
    # Figure out the data format for every input
    # =========================
    test_snps = _snps_fixup(test_snps, count_A1=count_A1)
    pheno = _pheno_fixup_and_check_missing(pheno, count_A1)
    covar = _pheno_fixup(covar, iid_if_none=pheno.iid, count_A1=count_A1)

    # =========================
    # Intersect and order individuals.
    # Make sure every K0_eigen individual
    # has data in test_snps, pheno, and covar.
    # =========================
    iid_count_before = K0_eigen.row_count
    test_snps, pheno, K0_eigen, covar = pstutil.intersect_apply(
        [test_snps, pheno, K0_eigen, covar]
    )
    assert (
        K0_eigen.row_count == iid_count_before
    ), "Must have test_snps, pheno, and covar data for each K0_eigen individual"

    # !!!cmk assert covar_by_chrom is None, "When 'leave_out_one_chrom' is False,
    #  'covar_by_chrom' must be None"
    # !!!cmk K0, K1, block_size = _set_block_size(K0, K1, mixing, GB_goal,
    #  force_full_rank, force_low_rank)

    # !!! cmk
    # if h2 is not None and not isinstance(h2, np.ndarray):
    #     h2 = np.repeat(h2, pheno.shape[1])

    # =========================
    # Read K0_eigen, covar, pheno into memory.
    # Next rotate covar and pheno.
    #
    # An "EigenReader" object includes both the vectors and values.
    # A Rotation object always includes rotated=eigenvectors * a.
    # If low-rank EigenReader, also includes double=a-eigenvectors*rotated.
    # =========================
    K0_eigen = K0_eigen.read(view_ok=True, order="A")
    covar = _covar_read_with_bias(covar)
    pheno = pheno.read(view_ok=True, order="A")

    covar_r = K0_eigen.rotate(covar)
    pheno_r = K0_eigen.rotate(pheno)

    # =========================
    # Find the K0+delta I with the best likelihood.
    # A KdI object includes
    #   * Sd = eigenvalues + delta
    #   * is_low_rank (True/False)
    #   * logdet (depends on is_low_rank) 
    # =========================
    K0_kdi = _find_best_kdi_as_needed(
        K0_eigen,
        covar,
        covar_r,
        pheno_r,
        use_reml=find_delta_via_reml,
        log_delta=log_delta, # optional
    )

    # =========================
    # Find A^T * K^-1 * B for covar and pheno.
    # Then find null likelihood for testing.
    # "AKB.from_rotated" works for both full and low-rank.
    # A AKB object includes
    #   * The AKB value
    #   * The KdI objected use to create it.
    # =========================
    covarKcovar, covarK = AKB.from_rotated(covar_r, K0_kdi, covar_r)
    phenoKpheno, _ = AKB.from_rotated(pheno_r, K0_kdi, pheno_r)
    covarKpheno, _ = AKB.from_rotated(covar_r, K0_kdi, pheno_r, aK=covarK)

    ll_null, beta, variance_beta = _loglikelihood(
        covar, phenoKpheno, covarKcovar, covarKpheno, use_reml=test_via_reml
    )

    # ==================================
    # X is the covariates (with bias) and one test SNP.
    # Create an X, XKX, and XKpheno where
    # the last part can be swapped for each test SNP.
    # ==================================
    cc = covar.sid_count  # number of covariates including bias
    # !!!cmk what if alt is not unique?
    xkx_sid = np.append(covar.sid, "alt")
    if test_via_reml:
        # Only need "X" for REML
        X = SnpData(
            val=np.full((covar.iid_count, len(xkx_sid)), fill_value=np.nan),
            iid=covar.iid,
            sid=xkx_sid,
        )
        X.val[:, :cc] = covar.val  # left
    else:
        X = None

    XKX = AKB.empty(row=xkx_sid, col=xkx_sid, kdi=K0_kdi)
    XKX[:cc, :cc] = covarKcovar  # upper left
    XKpheno = AKB.empty(xkx_sid, pheno_r.rotated.col, kdi=K0_kdi)
    XKpheno[:cc, :] = covarKpheno  # upper

    # ==================================
    # Test SNPs in batches
    # ==================================
    # !!!cmk really do this in batches in different processes
    batch_size = 1000  # !!!cmk const
    result_list = []
    for sid_start in range(0, test_snps.sid_count, batch_size):

        # ==================================
        # Read and standardize a batch of test SNPs. Then rotate.
        # Find A^T * K^-1 * B for covar & pheno vs. the batch
        # ==================================
        alt_batch = (
            test_snps[:, sid_start : sid_start + batch_size].read().standardize()
        )
        alt_batch_r = K0_eigen.rotate(alt_batch)

        covarKalt_batch, _ = AKB.from_rotated(covar_r, K0_kdi, alt_batch_r, aK=covarK)
        alt_batchKy, alt_batchK = AKB.from_rotated(alt_batch_r, K0_kdi, pheno_r)

        # ==================================
        # For each test SNP in the batch
        # ==================================
        for i in range(alt_batch.sid_count):
            alt_r = alt_batch_r[i]

            # ==================================
            # Find alt^T * K^-1 * alt for the test SNP.
            # Fill in last value of X, XKX and XKpheno
            # with the alt value.
            # ==================================
            altKalt, _ = AKB.from_rotated(
                alt_r, K0_kdi, alt_r, aK=alt_batchK[:, i : i + 1].read(view_ok=True)
            )

            if test_via_reml:  # Only need "X" for REML
                X.val[:, cc:] = alt_batch.val[:, i : i + 1]  # right

            XKX[:cc, cc:] = covarKalt_batch[:, i : i + 1]  # upper right
            XKX[cc:, :cc] = XKX[:cc, cc:].T  # lower left
            XKX[cc:, cc:] = altKalt  # lower right

            XKpheno[cc:, :] = alt_batchKy[i : i + 1, :]  # lower

            # ==================================
            # Find likelihood with test SNP and score.
            # ==================================
            # O(sid_count * (covar+1)^6)
            ll_alt, beta, variance_beta = _loglikelihood(
                X, phenoKpheno, XKX, XKpheno, use_reml=test_via_reml
            )
            test_statistic = ll_alt - ll_null
            result_list.append(
                {
                    "PValue": stats.chi2.sf(2.0 * test_statistic, df=1),
                    "SnpWeight": beta,
                    "SnpWeightSE": np.sqrt(variance_beta),
                }
            )

        dataframe = _create_dataframe().append(result_list, ignore_index=True)
        dataframe["sid_index"] = range(test_snps.sid_count)
        dataframe["SNP"] = test_snps.sid
        dataframe["Chr"] = test_snps.pos[:, 0]
        dataframe["GenDist"] = test_snps.pos[:, 1]
        dataframe["ChrPos"] = test_snps.pos[:, 2]
        dataframe["Nullh2"] = np.zeros(test_snps.sid_count) + K0_kdi.h2
        # !!!cmk in lmmcov, but not lmm
        # dataframe['SnpFractVarExpl'] = np.sqrt(fraction_variance_explained_beta[:,0])
        # !!!cmk Feature not supported. could add "0"
        # dataframe['Mixing'] = np.zeros((len(sid))) + 0

    dataframe.sort_values(by="PValue", inplace=True)
    dataframe.index = np.arange(len(dataframe))

    if output_file_name is not None:
        dataframe.to_csv(output_file_name, sep="\t", index=False)

    return dataframe


def _pheno_fixup_and_check_missing(pheno, count_A1):
    pheno = _pheno_fixup(pheno, count_A1=count_A1).read()
    good_values_per_iid = (pheno.val == pheno.val).sum(axis=1)
    assert not np.any(
        (good_values_per_iid > 0) * (good_values_per_iid < pheno.sid_count)
    ), "With multiple phenotypes, an individual's values must either be all missing or have no missing."
    # !!!cmk multipheno
    # drop individuals with no good pheno values.
    pheno = pheno[good_values_per_iid > 0, :]

    assert pheno.sid_count >= 1, "Expect at least one phenotype"
    assert pheno.sid_count == 1, "currently only have code for one pheno"

    return pheno


def _covar_read_with_bias(covar):
    covar_val0 = covar.read(view_ok=True, order="A").val
    covar_val1 = np.c_[
        covar_val0, np.ones((covar.iid_count, 1))
    ]  # view_ok because np.c_ will allocation new memory
    # !!!cmk what is "bias' is already used as column name
    covar_and_bias = SnpData(
        iid=covar.iid,
        sid=list(covar.sid) + ["bias"],
        val=covar_val1,
        name=f"{covar}&bias",
    )
    return covar_and_bias


# !!!cmk needs better name
class KdI:
    def __init__(self, eigendata, h2=None, log_delta=None, delta=None):
        assert (
            sum([h2 is not None, log_delta is not None, delta is not None]) == 1
        ), "Exactly one of h2, etc should have a value"
        if h2 is not None:
            self.h2 = h2
            self.delta = 1.0 / h2 - 1.0
            self.log_delta = np.log(self.delta)
        elif log_delta is not None:
            self.log_delta = log_delta
            self.delta = np.exp(log_delta)
            self.h2 = 1.0 / (self.delta + 1)
        elif delta is not None:
            self.delta = delta
            self.log_delta = np.log(delta)
            self.h2 = 1.0 / (self.delta + 1)
        else:
            assert False, "real assert"

        self.row_count = eigendata.row_count
        self.row = eigendata.row
        self.is_low_rank = eigendata.is_low_rank
        self.logdet, self.Sd = eigendata.logdet(self.delta)


# !!!cmk move to PySnpTools
def AK(a_r, kdi, aK=None):
    if aK is None:
        return PstData(val=a_r.rotated.val / kdi.Sd, row=a_r.rotated.row, col=a_r.rotated.col)
    else:
        return aK


# !!!cmk move to PySnpTools
class AKB(PstData):
    def __init__(self, val, row, col, kdi):
        super().__init__(val=val, row=row, col=col)
        self.kdi = kdi

    @staticmethod
    def from_rotated(a_r, kdi, b_r, aK=None):
        aK = AK(a_r, kdi, aK)

        val = aK.val.T.dot(b_r.rotated.val)
        if kdi.is_low_rank:
            val += a_r.double.val.T.dot(b_r.double.val) / kdi.delta
        result = AKB(val=val, row=a_r.rotated.col, col=b_r.rotated.col, kdi=kdi)
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
        # !!!cmk may want to check that the kdi's are equal
        self.val[key] = value.val

    def __getitem__(self, index):
        # !!!cmk fast enough?
        result0 = super(AKB, self).__getitem__(index).read(view_ok=True)
        result = AKB(val=result0.val, row=result0.row, col=result0.col, kdi=self.kdi)
        return result  # !!! cmk right type?

    @property
    def T(self):
        return AKB(val=self.val.T, row=self.col, col=self.row, kdi=self.kdi)


# !!!cmk change use_reml etc to 'use_reml'
def _find_h2(
    eigendata, X, X_r, pheno_r, use_reml, nGridH2=10, minH2=0.0, maxH2=0.99999
):
    # !!!cmk log delta is used here. Might be better to use findH2, but if so will need to normalized G so that its kdi's diagonal would sum to iid_count
    logging.info("searching for delta/h2/logdelta")

    resmin = [None]

    def f(x, resmin=resmin, **kwargs):
        # This kdi is Kg+delta I
        kdi = KdI(eigendata, h2=x)
        # aKb is  a.T * kdi^-1 * b
        phenoKpheno, _ = AKB.from_rotated(pheno_r, kdi, pheno_r)
        XKX, XK = AKB.from_rotated(X_r, kdi, X_r)
        XKpheno, _ = AKB.from_rotated(X_r, kdi, pheno_r, aK=XK)

        nLL, _, _ = _loglikelihood(X, phenoKpheno, XKX, XKpheno, use_reml=use_reml)
        nLL = -nLL  # !!!cmk
        if (resmin[0] is None) or (nLL < resmin[0]["nLL"]):
            resmin[0] = {"nLL": nLL, "h2": x}
        logging.debug(f"search\t{x}\t{nLL}")
        return nLL

    _ = minimize1D(f=f, nGrid=nGridH2, minval=0.00001, maxval=maxH2)
    return resmin[0]


def _eigen_from_akb(akb, keep_above=np.NINF):
    # !!!cmk check that square aKa not just aKb???
    w, v = np.linalg.eigh(akb.val)  # !!! cmk do SVD sometimes?
    eigen = EigenData(values=w, vectors=v, row=akb.row)
    if keep_above > np.NINF:
        eigen = eigen[:, eigen.values > keep_above].read(view_ok=True)
    return eigen


def _eigen_from_xtx(xtx):
    # !!!cmk check that square aKa not just aKb???
    w, v = np.linalg.eigh(xtx.val)  # !!! cmk do SVD sometimes?
    eigen = EigenData(values=w, vectors=v, row=xtx.row)
    return eigen


def _common_code(phenoKpheno, XKX, XKpheno):  # !!! cmk rename
    # _cmk_common_code(phenoKpheno, XKX, XKpheno)

    # !!!cmk may want to check that all three kdi's are equal
    # !!!cmk may want to check that all three kdi's are equal

    eigen_xkx = _eigen_from_akb(XKX, keep_above=1e-10)
    kd0 = KdI(eigen_xkx, delta=0)
    XKpheno_r = eigen_xkx.rotate(XKpheno)
    XKphenoK = AK(XKpheno_r, kd0)
    beta = eigen_xkx.rotate(XKphenoK)
    beta = eigen_xkx.vectors.dot(
        eigen_xkx.rotate(XKpheno).rotated.val.reshape(-1) / eigen_xkx.values
    )

    r2 = float(phenoKpheno.val - XKpheno.val.reshape(-1).dot(beta))

    return r2, beta, eigen_xkx


def _loglikelihood(X, phenoKpheno, XKX, XKpheno, use_reml):
    if use_reml:
        nLL, beta = _loglikelihood_reml(X, phenoKpheno, XKX, XKpheno)
        return nLL, beta, np.nan
    else:
        return _loglikelihood_ml(phenoKpheno, XKX, XKpheno)


def _loglikelihood_reml(X, phenoKpheno, XKX, XKpheno):
    r2, beta, eigen_xkx = _common_code(phenoKpheno, XKX, XKpheno)
    kdi = phenoKpheno.kdi  # !!!cmk may want to check that all three kdi's are equal

    # !!!cmk isn't this a kernel?
    XX = PstData(val=X.val.T.dot(X.val), row=X.sid, col=X.sid)
    eigen_xx = _eigen_from_xtx(XX)
    logdetXX, _ = eigen_xx.logdet()

    logdetXKX, _ = eigen_xkx.logdet()
    sigma2 = r2 / (X.shape[0] - X.shape[1])
    nLL = 0.5 * (
        kdi.logdet
        + logdetXKX
        - logdetXX
        + (X.shape[0] - X.shape[1]) * (np.log(2.0 * np.pi * sigma2) + 1)
    )
    assert np.all(
        np.isreal(nLL)
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta


def _loglikelihood_ml(phenoKpheno, XKX, XKpheno):
    r2, beta, eigen_xkx = _common_code(phenoKpheno, XKX, XKpheno)
    kdi = phenoKpheno.kdi  # !!!cmk may want to check that all three kdi's are equal
    sigma2 = r2 / kdi.row_count
    nLL = 0.5 * (kdi.logdet + kdi.row_count * (np.log(2.0 * np.pi * sigma2) + 1))
    assert np.all(
        np.isreal(nLL)
    ), "nLL has an imaginary component, possibly due to constant covariates"
    variance_beta = (
        kdi.h2
        * sigma2
        * (eigen_xkx.vectors / eigen_xkx.values * eigen_xkx.vectors).sum(-1)
    )
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta, variance_beta


# Returns a kdi that is the original Kg + delta I
def _find_best_kdi_as_needed(
    eigendata, covar, covar_r, pheno_r, use_reml, log_delta=None
):
    if log_delta is None:
        # cmk As per the paper, we optimized delta with use_reml=True, but
        # cmk we will later optimize beta and find log likelihood with ML (use_reml=False)
        h2 = _find_h2(
            eigendata, covar, covar_r, pheno_r, use_reml=use_reml, minH2=0.00001
        )["h2"]
        return KdI(eigendata, h2=h2)
    else:
        # !!!cmk internal/external doesn't matter if full rank, right???
        return KdI(eigendata, log_delta=log_delta)


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


# !!!cmk move to pysnptools
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
            K0.snpreader.read().standardize(K0.standardizer).val, full_matrices=False
        )
        if np.any(sqrt_values < -0.1):
            logging.warning("kernel contains a negative Eigenvalue")
        eigen = EigenData(values=sqrt_values * sqrt_values, vectors=vectors, row=K0.iid)
    else:
        # !!!cmk understand _read_kernel, _read_with_standardizing

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
