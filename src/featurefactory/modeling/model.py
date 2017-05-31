import os
import traceback
import sys
import numpy as np
import sklearn.metrics
from collections import defaultdict
from sklearn.preprocessing import label_binarize, LabelEncoder
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from sklearn.externals import joblib

from featurefactory.modeling.metrics import Metric, MetricList
from featurefactory.util import RANDOM_STATE

# automl
from autosklearn.classification import AutoSklearnClassifier
from autosklearn.regression import AutoSklearnRegressor
from featurefactory.modeling.scorers import rmsle_autoscorer, ndcg_autoscorer

class Model(object):
    """Versatile modeling object.

    Handles classification and regression problems and computes variety of
    performance metrics.

    Parameters
    ----------
    problem_type : str
        One of "classification" or "regression"
    """

    CLASSIFICATION = "classification"
    REGRESSION     = "regression"

    CLASSIFICATION_SCORING = [
        { "name" : "Accuracy"  , "scoring" : "accuracy" },
        { "name" : "Precision" , "scoring" : "precision" },
        { "name" : "Recall"    , "scoring" : "recall" },
        { "name" : "ROC AUC"   , "scoring" : "roc_auc" },
    ]
    REGRESSION_SCORING = [
        { "name" : "Root Mean Squared Error" , "scoring" : "root_mean_squared_error" },
        { "name" : "R-squared"          , "scoring" : "r2" },
    ]

    BINARY_METRIC_AGGREGATION = "micro"
    MULTICLASS_METRIC_AGGREGATION = "micro"

    def __init__(self, problem_type):
        self.problem_type = problem_type

        if self._is_classification():
            self.model = DecisionTreeClassifier(random_state=RANDOM_STATE+1)
        elif self._is_regression():
            self.model = DecisionTreeRegressor(random_state=RANDOM_STATE+2)
        else:
            raise NotImplementedError

    def compute_metrics(self, X, Y, kind="cv", **kwargs):
        if kind=="cv":
            return self.compute_metrics_cv(X, Y, **kwargs)
        elif kind=="train_test":
            return self.compute_metrics_train_test(X, Y, **kwargs)
        else:
            raise ValueError("Bad kind: {}".format(kind))

    def compute_metrics_cv(self, X, Y):
        """Compute cross-validated metrics.

        Trains this model on data X with labels Y.

        Returns a MetricList with the name, scoring type, and value for each
        Metric. Note that these values may be numpy floating points, and should
        be converted prior to insertion in a database.

        Parameters
        ----------
        X : numpy array-like or pd.DataFrame
            data
        Y : numpy array-like or pd.DataFrame or pd.DataSeries
            labels
        """

        scorings, scorings_ = self._get_scorings()

        # compute scores
        scores = self.cv_score_mean(X, Y, scorings_)

        # unpack into MetricList
        metric_list = self.scores_to_metriclist(scorings, scores)
        return metric_list

    def compute_metrics_train_test(self, X, Y, n):
        """Compute metrics on test set.
        """

        X, Y = Model._format_matrices(X, Y)

        X_train, Y_train = X[:n], Y[:n]
        X_test, Y_test = X[n:], Y[n:]

        scorings, scorings_ = self._get_scorings()

        # Determine binary/multiclass classification
        classes = np.unique(Y)
        params = self._get_params(classes)

        # fit model on entire training set
        self.model.fit(X_train, Y_train)

        scores = {}
        for scoring in scorings_:
            scores[scoring] = self._do_scoring(scoring, params, self.model,
                    X_test, Y_test)

        metric_list = self.scores_to_metriclist(scorings, scores)
        return metric_list

    def _do_scoring(self, scoring, params, model, X_test, Y_test,
            failure_value=None):
        # Make and evaluate predictions. Note that ROC AUC may raise
        # exception if somehow we only have examples from one class in
        # a given fold.
        Y_test_transformed = params[scoring]["pred_transformer"](Y_test)
        Y_test_pred = params[scoring]["predictor"](model, X_test)

        try:
            score = params[scoring]["scorer"](Y_test_transformed, Y_test_pred)
        except ValueError as e:
            score = failure_value
            print(traceback.format_exc(), file=sys.stderr)
            raise RuntimeError

        return score

    def cv_score_mean(self, X, Y, scorings):
        """Compute mean score across cross validation folds.

        Split data and labels into cross validation folds and fit the model for
        each fold. Then, for each scoring type in scorings, compute the score.
        Finally, average the scores across folds. Returns a dictionary mapping
        scoring to score.

        Parameters
        ----------
        X : numpy array-like
            data
        Y : numpy array-like
            labels
        scorings : list of str
            scoring types
        """

        X, Y = Model._format_matrices(X, Y)

        scorings = list(scorings)

        # Determine binary/multiclass classification
        classes = np.unique(Y)
        params = self._get_params(classes)

        if self._is_classification():
            kf = StratifiedKFold(shuffle=True, random_state=RANDOM_STATE+3)
        else:
            kf = KFold(shuffle=True, random_state=RANDOM_STATE+4)

        # Split data, train model, and evaluate metric. We fit the model just
        # once per fold.
        scoring_outputs = defaultdict(lambda : [])
        for train_inds, test_inds in kf.split(X, Y):
            X_train, X_test = X[train_inds], X[test_inds]
            Y_train, Y_test = Y[train_inds], Y[test_inds]

            self.model.fit(X_train, Y_train)

            for scoring in scorings:
                score = self._do_scoring(scoring, params, self.model, X_test,
                        Y_test, failure_value=np.nan)
                scoring_outputs[scoring].append(score)

        for scoring in scoring_outputs:
            score_mean = np.nanmean(scoring_outputs[scoring])
            if np.isnan(score_mean):
                score_mean = None
            scoring_outputs[scoring] = score_mean

        return scoring_outputs

    def scores_to_metriclist(self, scorings, scores):
        metric_list = MetricList()
        for v in scorings:
            name    = v["name"]
            scoring = v["scoring"]

            if scoring in scores:
                value = scores[scoring]
            else:
                value = None

            metric_list.append(Metric(name, scoring, value))

        return metric_list

    def _is_classification(self):
        return self.problem_type == "classification"

    def _is_regression(self):
        return self.problem_type == "regression"

    def _get_params(self, classes):
        n_classes = len(classes)
        is_binary = n_classes == 2
        if is_binary:
            metric_aggregation = Model.BINARY_METRIC_AGGREGATION
        else:
            metric_aggregation = Model.MULTICLASS_METRIC_AGGREGATION

        # Determine predictor (labels, label probabilities, or values) and
        # scoring function.

        # predictors
        def predict(model, X_test):
            return model.predict(X_test)
        def predict_prob(model, X_test):
            return model.predict_proba(X_test)

        # transformers
        def noop(y_true):
            return y_true
        def transformer_binarize(y_true):
            return label_binarize(y_true, classes=classes)

        # scorers
        # nothing here

        params = {
            "accuracy" : {
                "predictor" : predict,
                "pred_transformer" : noop,
                "scorer" : sklearn.metrics.accuracy_score,
            },
            "precision" : {
                "predictor" : predict,
                "pred_transformer" : noop,
                "scorer" : lambda y_true, y_pred: sklearn.metrics.precision_score(
                        y_true, y_pred, average=metric_aggregation),
            },
            "recall" : {
                "predictor" : predict,
                "pred_transformer" : noop,
                "scorer" : lambda y_true, y_pred: sklearn.metrics.recall_score(
                        y_true, y_pred, average=metric_aggregation),
            },
            "roc_auc" : {
                "predictor" : predict if is_binary else predict_prob,
                "pred_transformer" : noop if is_binary else transformer_binarize,
                "scorer" : lambda y_true, y_pred: sklearn.metrics.roc_auc_score(
                    y_true, y_pred, average=metric_aggregation),
            },
            "root_mean_squared_error" : {
                "predictor" : predict,
                "pred_transformer" : noop,
                "scorer" : lambda y_true, y_pred:
                    np.sqrt(sklearn.metrics.mean_squared_error(y_true,
                        y_pred)),
            },
            "r2" : {
                "predictor" : predict,
                "pred_transformer" : noop,
                "scorer" : sklearn.metrics.r2_score
            },
        }

        return params

    def _get_scorings(self):
        """Get scorings for this problem type.

        Returns
        -------
        scorings : list of dict
            Information on metric name and associated "scoring" as defined in
            sklearn.metrics
        scorings_ : list
            List of "scoring" as defined in sklearn.metrics. This is a "utility
            variable" that can be used where we just need the names of the
            scoring functions and not the more complete information.
        """
        # scoring_types maps user-readable name to `scoring`, as argument to
        # cross_val_score
        # See also http://scikit-learn.org/stable/modules/model_evaluation.html#scoring-parameter
        if self._is_classification():
            scorings = Model.CLASSIFICATION_SCORING
            scorings_= [s["scoring"] for s in scorings]
        elif self._is_regression():
            scorings = Model.REGRESSION_SCORING
            scorings_= [s["scoring"] for s in scorings]
        else:
            raise NotImplementedError

        return scorings, scorings_

    @staticmethod
    def _format_matrices(X, Y):
        X = Model._formatX(X)
        Y = Model._formatY(Y)
        return X, Y

    @staticmethod
    def _formatX(X):
        # ensure that we use np for everything
        # use np.float64 for all elements
        # *don't* use 1d array for X
        X = np.asfarray(X)
        if X.ndim == 1:
            X = X.reshape(-1,1)

        return X

    @staticmethod
    def _formatY(Y):
        # TODO: detect if we need to use a LabelEncoder for Y
        # ensure that we use np for everything
        # use np.float64 for all elements
        # *do* use 1d array for Y
        Y = np.asfarray(Y)
        if Y.ndim > 1 and Y.shape[1] > 1:
            raise ValueError("Target matrix has too many columns: {}"
                    .format(Y.shape[1]))
        Y = Y.ravel()
        return Y

class AutoModel(Model):
    TIME_LEFT_FOR_THIS_TASK=90
    PER_RUN_TIME_LIMIT=10
    INITIAL_CONFIGURATIONS_VIA_METALEARNING=0

    def __init__(self, problem_type, metric, **kwargs):
        super().__init__(problem_type)

        if "time_left_for_this_task" in kwargs:
            self.time_left_for_this_task = kwargs["time_left_for_this_task"]
        else:
            self.time_left_for_this_task = AutoModel.TIME_LEFT_FOR_THIS_TASK

        if "per_run_time_limit" in kwargs:
            self.per_run_time_limit = kwargs["per_run_time_limit"]
        else:
            self.per_run_time_limit = AutoModel.PER_RUN_TIME_LIMIT

        if "initial_configurations_via_metalearning" in kwargs:
            self.initial_configurations_via_metalearning= \
                kwargs["initial_configurations_via_metalearning"]
        else:
            self.initial_configurations_via_metalearning= \
                AutoModel.INITIAL_CONFIGURATIONS_VIA_METALEARNING

        # ~hack~
        # set custom scorers. really, we should include this in the database
        if self._is_classification():
            self.scorer = ndcg_autoscorer
        elif self._is_regression():
            self.scorer = rmsle_autoscorer
        else:
            raise NotImplementedError

        if self._is_classification():
            self.model = AutoSklearnClassifier(
                time_left_for_this_task =
                    self.time_left_for_this_task,
                per_run_time_limit =
                    self.per_run_time_limit,
                initial_configurations_via_metalearning =
                    self.initial_configurations_via_metalearning,
                seed = RANDOM_STATE+1)
        elif self._is_regression():
            self.model = AutoSklearnRegressor(
                time_left_for_this_task =
                    self.time_left_for_this_task,
                per_run_time_limit =
                    self.per_run_time_limit,
                initial_configurations_via_metalearning =
                    self.initial_configurations_via_metalearning,
                seed = RANDOM_STATE+2)
        else:
            raise NotImplementedError

    def fit(self, X_train, Y_train, **kwargs):
        X_train = Model._formatX(X_train)
        
        if self._is_classification() and \
            np.unique(Y_train) > 2:
            self.le = LabelEncoder()
            self.le.fit(Y_train)
            Y_train = self.le.transform(Y_train)
        Y_train = Model._formatY(Y_train)

        self.model.fit(X_train, Y_train, metric=self.scorer, **kwargs)

    def predict(self, X_test):
        X_test = Model._formatX(X_test)
        Y_test_pred = self.model.predict(X_test)
        if self._is_classification() and \
            np.unique(Y_test_pred) > 2:
            # TODO this fails if <=2 classes are actually predicted. Should
            # store whether it is multiclass classification as class member.
            return self.le.inverse_transform(Y_test_pred)
        else:
            return Y_test_pred

    def predict_proba(self, X_test):
        X_test = Model._formatX(X_test)
        Y_test_pred_proba = self.model.predict_proba(X_test)
        return Y_test_pred_proba

    def score(self, X_test, Y_test):
        X_test = Model._formatX(X_test)
        Y_test = Model._formatY(Y_test)
        Y_test_pred = self.predict(X_test)
        score = self.scorer(Y_test, Y_test_pred)
        return score

    def dump(self, absname):
        joblib.dump(self.model, absname)
        print("Model dumped to {}".format(absname))

    def load(self, absname):
        if not os.path.exists(absname):
            raise ValueError("Couldn't find model at {}".format(absname))
        self.model = joblib.load(absname)
        print("Model loaded successfully.")
