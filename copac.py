#!/usr/bin/env python3

"""
COPAC: Correlation Partition Clustering
"""

# Author: Roman Feldbauer <roman.feldbauer@ofai.at>
#         ... <>
#         ... <>
#         ... <>
#
# License: ...
from multiprocessing import cpu_count

import numpy as np
from scipy import linalg as LA
from scipy.spatial.distance import squareform

from sklearn.base import BaseEstimator, ClusterMixin
from sklearn.cluster.dbscan_ import dbscan
from sklearn.neighbors import NearestNeighbors
from sklearn.utils import check_array


def _cdist(P, Q, Mhat_P):
    """ Correlation distance between P and Q (not symmetric).

    Notes
    -----
    The squareroot of cdist is taken later. The advantage here is to
    save some computation, as we can first take the maximum of
    two cdists, and then take the root of the 'winner' only.
    """
    PQ_diff = P[np.newaxis, :] - Q
    return (PQ_diff @ Mhat_P * PQ_diff).sum(axis=1)


def copac(X, k=10, mu=5, eps=0.5, alpha=0.85, metric='euclidean',
          metric_params=None, algorithm='auto', leaf_size=30, p=None,
          n_jobs=1, sample_weight=None):
    """Perform COPAC clustering from vector array.
    Read more in the :ref:`User Guide <copac>`.

    Parameters
    ----------
    X : array of shape (n_samples, n_features)
        A feature array.
    k : int, optional, default=10
        Size of local neighborhood for local correlation dimensionality.
        The paper suggests k >= 3 * n_features.
    mu : int, optional, default=5
        Minimum number of points in a cluster with mu <= k.
    eps : float, optional, default=0.5
        Neighborhood predicate, so that neighbors are closer than `eps`.
    alpha : float in ]0,1[, optional, default=0.85
        Threshold of how much variance needs to be explained by Eigenvalues.
        Assumed to be robust in range 0.8 <= alpha <= 0.9 [see Ref.]
    metric : string, or callable
        The metric to use when calculating distance between instances in a
        feature array. If metric is a string or callable, it must be one of
        the options allowed by sklearn.metrics.pairwise.pairwise_distances
        for its metric parameter.
        If metric is "precomputed", `X` is assumed to be a distance matrix and
        must be square.
    metric_params : dict, optional
        Additional keyword arguments for the metric function.
    algorithm : {'auto', 'ball_tree', 'kd_tree', 'brute'}, optional
        The algorithm to be used by the scikit-learn NearestNeighbors module
        to compute pointwise distances and find nearest neighbors.
        See NearestNeighbors module documentation for details.
    leaf_size : int, optional (default = 30)
        Leaf size passed to BallTree or cKDTree. This can affect the speed
        of the construction and query, as well as the memory required
        to store the tree. The optimal value depends
        on the nature of the problem.
    p : float, optional
        The power of the Minkowski metric to be used to calculate distance
        between points.
    n_jobs : int, optional, default=1
        Number of parallel processes. Use all cores with n_jobs=-1.
    sample_weight : None
        Currently ignored

    Returns
    -------
    labels : array [n_samples]
        Cluster labels for each point. Noisy samples are given the label -1.

    References
    ----------
    Elke Achtert, Christian Bohm, Hans-Peter Kriegel, Peer Kroger,
    A. Z. (n.d.). Robust, complete, and efficient correlation
    clustering. In Proceedings of the Seventh SIAM International
    Conference on Data Mining, April 26-28, 2007, Minneapolis,
    Minnesota, USA (2007), pp. 413–418.
    """
    X = check_array(X)
    n, d = X.shape
    y = -np.ones(n, dtype=np.int)
    if n_jobs == -1:
        n_jobs = cpu_count()

    # Calculating M^ just once requires lots of memory...
    lambda_ = np.zeros(n, dtype=int)
    M_hat = list()

    # Get nearest neighbors
    nn = NearestNeighbors(n_neighbors=k, metric=metric, algorithm=algorithm,
                          leaf_size=leaf_size, metric_params=metric_params,
                          p=p, n_jobs=n_jobs)
    nn.fit(X)
    knns = nn.kneighbors(return_distance=False)
    for P, knn in enumerate(knns):
        N_P = X[knn]

        # Corr. cluster cov. matrix
        Sigma = np.cov(N_P[:, :], rowvar=False, ddof=0)

        # Decompose spsd matrix, and sort Eigenvalues descending
        E, V = LA.eigh(Sigma)
        del Sigma
        E = np.sort(E)[::-1]

        # Local correlation dimension
        explanation_portion = np.cumsum(E) / E.sum()
        lambda_P = np.searchsorted(explanation_portion, alpha, side='left')
        lambda_P += 1
        lambda_[P] = lambda_P
        # Correlation distance matrix
        E_hat = (np.arange(1, d + 1) > lambda_P).astype(int)
        M_hat.append(V @ np.diag(E_hat) @ V.T)

    # Group pts by corr. dim.
    argsorted = np.argsort(lambda_)
    edges, _ = np.histogram(lambda_[argsorted], bins=np.arange(1, d + 2))
    Ds = np.split(argsorted, np.cumsum(edges))
    # Loop over partitions according to local corr. dim.
    max_label = 0
    used_y = np.zeros_like(y, dtype=bool)
    for D in Ds:
        n_D = D.shape[0]
        cdist_P = -np.ones(n_D * (n_D - 1) // 2, dtype=np.float)
        cdist_Q = -np.ones((n_D, n_D), dtype=np.float)
        start = 0
        # Calculate triu part of distance matrix
        for i in range(0, n_D - 1):
            p = D[i]
            # Vectorized inner loop
            q = D[i + 1:n_D]
            stop = start + n_D - i - 1
            cdist_P[start:stop] = _cdist(X[p], X[q], M_hat[p])
            start = stop
        # Calculate tril part of distance matrix
        for i in range(1, n_D):
            q = D[i]
            p = D[0:i]
            cdist_Q[i, :i] = _cdist(X[q], X[p], M_hat[q])
        # Extract tril to 1D array
        # TODO simplify...
        cdist_Q = cdist_Q.T[np.triu_indices_from(cdist_Q, k=1)]
        cdist = np.block([[cdist_P], [cdist_Q]])
        # Square root of the higher value of cdist_P, cdist_Q
        cdist = np.sqrt(cdist.max(axis=0))

        # Perform DBSCAN with full distance matrix
        cdist = squareform(cdist)
        clust = dbscan(X=cdist, eps=eps, min_samples=mu,
                       metric='precomputed', n_jobs=n_jobs)
        _, labels = clust
        # Each DBSCAN run is unaware of previous ones,
        # so we need to keep track of previous cluster IDs
        y_D = labels + max_label
        new_labels = np.unique(labels[labels >= 0]).size
        max_label += new_labels
        # TODO check correct indexing of label array `y`
        y[D] = y_D
        used_y[D] = True
    assert np.all(used_y), "Not all samples got labels!"
    return y


class COPAC(BaseEstimator, ClusterMixin):
    """Perform COPAC clustering from vector array.
    Read more in the :ref:`User Guide <copac>`.

    Parameters
    ----------
    k : int, optional, default=10
        Size of local neighborhood for local correlation dimensionality.
        The paper suggests k >= 3 * n_features.
    mu : int, optional, default=5
        Minimum number of points in a cluster with mu <= k.
    eps : float, optional, default=0.5
        Neighborhood predicate, so that neighbors are closer than `eps`.
    alpha : float in ]0,1[, optional, default=0.85
        Threshold of how much variance needs to be explained by Eigenvalues.
        Assumed to be robust in range 0.8 <= alpha <= 0.9 [see Ref.]
    metric : string, or callable
        The metric to use when calculating distance between instances in a
        feature array. If metric is a string or callable, it must be one of
        the options allowed by sklearn.metrics.pairwise.pairwise_distances
        for its metric parameter.
        If metric is "precomputed", `X` is assumed to be a distance matrix and
        must be square.
    metric_params : dict, optional
        Additional keyword arguments for the metric function.
    algorithm : {'auto', 'ball_tree', 'kd_tree', 'brute'}, optional
        The algorithm to be used by the scikit-learn NearestNeighbors module
        to compute pointwise distances and find nearest neighbors.
        See NearestNeighbors module documentation for details.
    leaf_size : int, optional (default = 30)
        Leaf size passed to BallTree or cKDTree. This can affect the speed
        of the construction and query, as well as the memory required
        to store the tree. The optimal value depends
        on the nature of the problem.
    p : float, optional
        The power of the Minkowski metric to be used to calculate distance
        between points.
    n_jobs : int, optional, default=1
        Number of parallel processes. Use all cores with n_jobs=-1.

    Attributes
    ----------
    labels_ : array, shape = [n_samples]
        Cluster labels for each point in the dataset given to fit().
        Noisy samples are given the label -1.

    Notes
    -----
    ...
    References
    ----------
    Elke Achtert, Christian Bohm, Hans-Peter Kriegel, Peer Kroger,
    A. Z. (n.d.). Robust, complete, and efficient correlation
    clustering. In Proceedings of the Seventh SIAM International
    Conference on Data Mining, April 26-28, 2007, Minneapolis,
    Minnesota, USA (2007), pp. 413–418.
    """

    def __init__(self, k=10, mu=5, eps=0.5, alpha=0.85,
                 metric='euclidean', metric_params=None, algorithm='auto',
                 leaf_size=30, p=None, n_jobs=1):
        self.k = k
        self.mu = mu
        self.eps = eps
        self.alpha = alpha
        self.metric = metric
        self.metric_params = metric_params
        self.algorithm = algorithm
        self.leaf_size = leaf_size
        self.p = p
        self.n_jobs = n_jobs

    def fit(self, X, y=None, sample_weight=None):  # @UnusedVariable pylint: disable=unused-argument
        """Perform COPAC clustering from features.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_features)
            A feature array.
        sample_weight : array, shape (n_samples,), optional
            Weight of each sample, such that a sample with a weight of at least
            ``min_samples`` is by itself a core sample; a sample with negative
            weight may inhibit its eps-neighbor from being core.
            Note that weights are absolute, and default to 1.
        y : Ignored
        """
        X = check_array(X)
        clust = copac(X, sample_weight=sample_weight,
                      **self.get_params())
        self.labels_ = clust  #pylint: disable=attribute-defined-outside-init
        return self

    def fit_predict(self, X, y=None, sample_weight=None):  #pylint: disable=arguments-differ
        """Performs clustering on X and returns cluster labels.

        Parameters
        ----------
        X : ndarray matrix of shape (n_samples, n_features)
            A feature array.
        sample_weight : array, shape (n_samples,), optional
            Weight of each sample, such that a sample with a weight of at least
            ``min_samples`` is by itself a core sample; a sample with negative
            weight may inhibit its eps-neighbor from being core.
            Note that weights are absolute, and default to 1.
        y : Ignored

        Returns
        -------
        y : ndarray, shape (n_samples,)
            cluster labels
        """
        self.fit(X, sample_weight=sample_weight)
        return self.labels_
