import logging

import numpy as np
import scipy as sp
import pandas as pd

from fastlmm.util.mingrid import minimize1D

class EigenPlusDelta:
    def __init__(self, eigen, delta):
        self.eigen = eigen
        self.delta = delta
        self.values = eigen.values + delta
        self.logdet = np.log(self.values).sum()
        if eigen.is_low_rank:
            self.logdet += (self.eigen.iid_count-self.eigen.eid_count) * np.log(delta)

class AK:
    def __init__(self, a_r, eigen_plus_delta):
        assert a_r.eigen is eigen_plus_delta.eigen, "Expect a_r and eigen_plus_delta to use the same Eigen"
        self.a_r = a_r
        self.eigen_plus_delta = eigen_plus_delta
        self.aK = a_r.rotated / eigen_plus_delta.values[:, np.newaxis]

    @property
    def eigen(self):
        return self.a_r.eigen

class AKB:
    def __init__(self, aK, b_r):
        assert aK.eigen is b_r.eigen, "Expect aK and b_r to be rotated by the same Eigen"
        self.aK = aK

        self.aKb = aK.aK.T @ b_r.rotated

        if aK.eigen.is_low_rank: 
            self.aKb += aK.a_r.double.T @ b_r.double / aK.eigen_plus_delta.delta

class Rotation:
    def __init__(self, eigen, data):
        self.eigen = eigen
        self.rotated = eigen.vectors.T @ data
        if eigen.is_low_rank:
            self.double = data - eigen.vectors @ self.rotated
        else:
            self.double = None

    def aKb(self, aK, b_r):
        val = aK.T @ b_r.val

        if kdi.is_low_rank:  # !!!cmklr
            val += a_r.double.val.T @ b_r.double.val / kdi.delta

        return val

        return self.rotated / values_plus_delta[:, np.newaxis]


class Eigen:
    def __init__(self, values, vectors):
        self.values = values
        self.vectors = vectors
        self.iid_count = self.vectors.shape[0]  # number of individuals
        self.eid_count = self.vectors.shape[1]  # number of eigenvalues (and eigenvectors)
        self.is_low_rank = self.eid_count < self.iid_count



def single_snp_simple(
    test_snps,
    test_snps_ids,
    pheno,
    K_eigen: Eigen,  # !!!cmk be consistant with K_eigen vs eigen_K etc
    covar,
    log_delta=None,
    _find_delta_via_reml=True,
    _test_via_reml=False,
):
    """
    test_snps should already be unit standardized()
    K should be a symmetric matrix (???cmk with diag sum=shape[0]???)
    with full-rank eigenvectors(???)
    covar should already have a column of 1's
    set log_delta to None for search
    """

    if _test_via_reml or _find_delta_via_reml:
        covarTcovar = covar.T @ covar
        eigenvalues_covarTcovar, eigenvectors_covarTcovar = np.linalg.eigh(
            covarTcovar
        )  # !!!cmk assumes full rank
        logdet_covarTcovar = np.log(
            eigenvalues_covarTcovar
        ).sum()  # cmk assumes full rank
    else:
        covarTcovar, logdet_covarTcovar = None, None

    covar_r = Rotation(K_eigen, covar)
    pheno_r = Rotation(K_eigen, pheno)

    if log_delta is None:
        h2 = _find_h2(
            K_eigen,
            covar_r,
            pheno_r,
            logdet_covarTcovar,
            use_reml=_find_delta_via_reml,
        )

        h2, _, delta = _hld(h2=h2)
    else:
        h2, _, delta = _hld(log_delta=log_delta)

    K_eigen_plus_delta = EigenPlusDelta(K_eigen, delta)

    covarK = AK(covar_r, K_eigen_plus_delta)
    phenoK = AK(pheno_r, K_eigen_plus_delta)

    covarKcovar = AKB(covarK, covar_r)
    phenoKpheno = AKB(phenoK, pheno_r)
    covarKpheno = AKB(covarK,  pheno_r)

    ll_null, _, _ = _loglikelihood(
        covarKcovar,
        phenoKpheno,
        covarKpheno,
        _test_via_reml,
        logdet_covarTcovar)

    result_list = []
    for test_snp_index in range(test_snps.shape[1]):
        alt = test_snps[:, test_snp_index : test_snp_index + 1]

        X = np.c_[covar, alt]
        X_r = Rotation(K_eigen, X)
        XK = AK(X_r, K_eigen_plus_delta)
        XKX = AKB(XK, X_r)

        # Only need XTX and "logdet_xtx" for REML
        if _test_via_reml:
            XTX = X.T @ X
            eigenvalues_XTX, _ = np.linalg.eigh(XTX)
            logdet_xtx = np.log(eigenvalues_XTX.sum())
        else:
            XTX = None
            logdet_xtx = None

        XKpheno = AKB(XK, pheno_r)

        # ==================================
        # Find likelihood with test SNP and score.
        # ==================================
        ll_alt, beta, variance_beta = _loglikelihood(
            XKX,
            phenoKpheno,
            XKpheno,
            _test_via_reml,
            logdet_xtx)

        test_statistic = ll_alt - ll_null

        result_list.append(
            {
                "SNP": test_snps_ids[test_snp_index],
                "PValue": sp.stats.chi2.sf(2.0 * test_statistic, df=1),
                "SnpWeight": beta[-1, 0],  # !!!cmk
                "SnpWeightSE": np.NaN  # !!!cmk0 np.sqrt(variance_beta[-1])
                if variance_beta is not None
                else None,
                # !!!cmk right name and place?
                "Nullh2": h2,
            }
        )

    df = pd.DataFrame(result_list)
    return df


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
    elif delta is not None:  # !!!cmk test
        delta = delta
        log_delta = np.log(delta) if delta != 0 else None
        h2 = 1.0 / (delta + 1)
    else:
        assert False, "real assert"
    return h2, log_delta, delta


def _find_h2(
    K_eigen,
    covar_r,
    pheno_r,
    logdet_covarTcovar,
    use_reml,
    nGridH2=10,
    minH2=0.00001,
    maxH2=0.99999,
):
    # !!!cmk log delta is used here. Might be better to use findH2, but if so will
    # need to normalized G so that its kdi's diagonal would sum to iid_count
    logging.info("searching for delta/h2/logdelta")

    resmin = [None]

    def f(h2, resmin=resmin, **kwargs):
        _, _, delta = _hld(h2)
        K_eigen_plus_delta = EigenPlusDelta(K_eigen, delta)

        covarK = AK(covar_r, K_eigen_plus_delta)
        phenoK = AK(pheno_r, K_eigen_plus_delta)

        covarKcovar = AKB(covarK, covar_r)
        phenoKpheno = AKB(phenoK, pheno_r)
        covarKpheno = AKB(covarK, pheno_r)

        nLL, _, _ = _loglikelihood(
            covarKcovar,
            phenoKpheno,
            covarKpheno,
            use_reml,
            logdet_covarTcovar)
        nLL = -nLL  # !!!cmk
        if (resmin[0] is None) or (nLL < resmin[0]["nLL"]):
            resmin[0] = {"nLL": nLL, "h2": h2}
        logging.debug(f"search\t{h2}\t{nLL}")
        return nLL

    _ = minimize1D(f=f, nGrid=nGridH2, minval=0.00001, maxval=maxH2)
    return resmin[0]["h2"]


def _loglikelihood(XKX, yKy, XKy, use_reml, logdet_xtx):
    assert XKX.aK.eigen_plus_delta is yKy.aK.eigen_plus_delta and yKy.aK.eigen_plus_delta is XKy.aK.eigen_plus_delta, "Expect XKX, yKy, and XKY to have the same eigen_plus_delta"

    if use_reml:  # !!!cmk don't trust the REML path
        nLL, beta = _loglikelihood_reml(
            XKX, yKy, XKy, logdet_xtx
        )
        return nLL, beta, None
    else:
        return _loglikelihood_ml(XKX, yKy, XKy)


# Note we have both XKX with XTX
def _loglikelihood_reml(
    XKX, yKy, XKy, logdet_xtx
):
    logdet = np.log(K_eigen_plus_delta).sum()

    (xkx_eigenvalues, _), beta, rss = _find_beta(yKy, XKX, XKy)
    logdet_xkx = np.log(xkx_eigenvalues.sum())

    X_row_less_col = X_row_count - XKX.shape[0]

    sigma2 = rss / X_row_less_col
    nLL = 0.5 * (
        logdet
        + logdet_xkx
        - logdet_xtx
        + X_row_less_col * (np.log(2.0 * np.pi * sigma2) + 1)
    )

    assert np.isreal(
        nLL
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta


def _loglikelihood_ml(XKX, yKy, XKy):
    logdet = XKX.aK.eigen_plus_delta.logdet
    eigen_plus_delta = XKX.aK.eigen_plus_delta
    h2, _, _ = _hld(delta=eigen_plus_delta.delta)
    iid_count = eigen_plus_delta.eigen.iid_count

    (eigen_xkx_values, eigen_xkx_vectors), beta, rss = _find_beta(yKy, XKX, XKy)

    sigma2 = rss / iid_count
    nLL = 0.5 * (logdet + iid_count * (np.log(2.0 * np.pi * sigma2) + 1))
    assert np.isreal(
        nLL
    ), "nLL has an imaginary component, possibly due to constant covariates"
    # This is a faster version of h2 * sigma2 * np.diag(LA.inv(XKX))
    # where h2*sigma2 is sigma2_g
    # !!!cmk kludge need to test these
    variance_beta = (
        h2 * sigma2 * (eigen_xkx_vectors / eigen_xkx_values * eigen_xkx_vectors).sum(-1)
    )
    # !!!cmk which is negative loglikelihood and which is LL?
    return -nLL, beta, variance_beta


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
    # or  beta = vectors @ (vectors.T @ (X.T @ y)/values) ???!!!cmk

    XKX_eigenvalues, XKX_eigenvectors = np.linalg.eigh(XKX.aKb)
    keep = XKX_eigenvalues > 1e-10
    XKX_eigenvalues = XKX_eigenvalues[keep]
    XKX_eigenvectors = XKX_eigenvectors[:, keep]

    # !!!cmk doesn't work with low-rank???
    beta = (XKX_eigenvectors / XKX_eigenvalues) @ XKX_eigenvectors.T @ XKy.aKb

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
    # ref2: beta is chosen s.t. y.T@X = X@beta.T@X aka X.T@X@beta
    # RSS = y.T @ y - 2*y.T @ (X @ beta) + y.T @ X @ beta)
    # RSS = y.T @ y - y.T @ X @ beta

    rss = float(yKy.aKb - XKy.aKb.T @ beta)

    return (XKX_eigenvalues, XKX_eigenvectors), beta, rss