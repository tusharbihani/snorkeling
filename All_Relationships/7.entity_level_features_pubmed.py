
# coding: utf-8

# # Data Preparation for True Relationship Prediction

# After predicting the co-occurence of candidates on the sentence level, the next step is to predict whether a candidate is a true relationship or just occured by chance. Through out this notebook the main events involved here are calculating summary statistics and obtaining the LSTM marginal probabilities.

# In[ ]:


get_ipython().run_line_magic('load_ext', 'autoreload')
get_ipython().run_line_magic('autoreload', '2')
get_ipython().run_line_magic('matplotlib', 'inline')

from collections import defaultdict
import csv
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tqdm
from scipy.stats import fisher_exact
import scipy
from sqlalchemy import and_
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import roc_curve, auc, confusion_matrix
from sklearn.model_selection import RandomizedSearchCV
import seaborn as sns


# In[ ]:


#Set up the environment
username = "danich1"
password = "snorkel"
dbname = "pubmeddb"

#Path subject to change for different os
database_str = "postgresql+psycopg2://{}:{}@/{}?host=/var/run/postgresql".format(username, password, dbname)
os.environ['SNORKELDB'] = database_str

from snorkel import SnorkelSession
session = SnorkelSession()


# In[ ]:


from snorkel.models import Candidate, candidate_subclass
from snorkel.learning.disc_models.rnn import reRNN


# In[ ]:


DiseaseGene = candidate_subclass('DiseaseGene', ['Disease', 'Gene'])


# # Count the Number of Sentences for Each Candidate

# For this block of code we are cycling through each disease-gene candidate in the database and counting the number of unique sentences and unique abstracts containing the specific candidate. **NOTE**: This section will quite a few hours to cycle through the entire database.

# In[ ]:


get_ipython().run_cell_magic('time', '', 'pair_to_pmids = defaultdict(set)\npair_to_sentences = defaultdict(set)\noffset = 0\nchunk_size = 1e5\n\nwhile True:\n    cands = session.query(DiseaseGene).limit(chunk_size).offset(offset).all()\n    \n    if not cands:\n        break\n        \n    for candidate in cands:\n        pair = candidate.Disease_cid, candidate.Gene_cid\n        pair_to_sentences[pair].add(candidate[0].get_parent().id)\n        pair_to_pmids[pair].add(candidate[0].get_parent().document_id)\n\n    offset+= chunk_size')


# In[ ]:


candidate_df = pd.DataFrame(
    map(lambda x: [x[0], x[1], len(pair_to_sentences[x]), len(pair_to_pmids[x])], pair_to_sentences),
    columns=["disease_id", "gene_id", "sentence_count", "abstract_count"]
    )


# # Perform Fisher Exact Test on each Co-Occurence

# Here we want to perform the fisher exact test for each disease-gene co-occurence. A more detailed explanation [here](https://github.com/greenelab/snorkeling/issues/26).

# In[ ]:


def diffprop(obs):
    """
    `obs` must be a 2x2 numpy array.

    Returns:
    delta
        The difference in proportions
    ci
        The Wald 95% confidence interval for delta
    corrected_ci
        Yates continuity correction for the 95% confidence interval of delta.
    """
    n1, n2 = obs.sum(axis=1)
    prop1 = obs[0,0] / n1.astype(np.float64)
    prop2 = obs[1,0] / n2.astype(np.float64)
    delta = prop1 - prop2

    # Wald 95% confidence interval for delta
    se = np.sqrt(prop1*(1 - prop1)/n1 + prop2*(1 - prop2)/n2)
    ci = (delta - 1.96*se, delta + 1.96*se)

    # Yates continuity correction for confidence interval of delta
    correction = 0.5*(1/n1 + 1/n2)
    corrected_ci = (ci[0] - correction, ci[1] + correction)

    return delta, ci, corrected_ci


# In[ ]:


total = candidate_df["sentence_count"].sum()
odds = []
p_val = []
expected = []
lower_ci = []
epsilon = 1e-36

for disease, gene in tqdm.tqdm(zip(candidate_df["disease_id"],candidate_df["gene_id"])):
    cond_df = candidate_df.query("disease_id == @disease & gene_id == @gene")
    a = cond_df["sentence_count"].values[0] + 1
                                        
    cond_df = candidate_df.query("disease_id != @disease & gene_id == @gene")
    b = cond_df["sentence_count"].sum() + 1
    
    cond_df = candidate_df.query("disease_id == @disease & gene_id != @gene")
    c = cond_df["sentence_count"].sum() + 1
    
    cond_df = candidate_df.query("disease_id != @disease & gene_id != @gene")
    d = cond_df["sentence_count"].sum() + 1
    
    c_table = np.array([[a, b], [c, d]])
    
    # Gather confidence interval
    delta, ci, corrected_ci = diffprop(c_table)
    lower_ci.append(ci[0])
    
    # Gather corrected odds ratio and p_values
    odds_ratio, p_value = fisher_exact(c_table, alternative='greater')
    odds.append(odds_ratio)
    p_val.append(p_value + epsilon)
    
    total_disease = candidate_df[candidate_df["disease_id"] == disease]["sentence_count"].sum()
    total_gene = candidate_df[candidate_df["gene_id"] == gene]["sentence_count"].sum()
    expected.append((total_gene * total_disease)/float(total))

candidate_df["nlog10_p_value"] = (-1 * np.log10(p_val))
candidate_df["co_odds_ratio"] = odds
candidate_df["co_expected_sen_count"] = expected
candidate_df["delta_lower_ci"] = lower_ci


# In[ ]:


candidate_df.sort_values("nlog10_p_value", ascending=False).head(1000)


# # Combine Sentence Marginal Probabilities

# In this section we incorporate the marginal probabilites that are calculated from the bi-directional LSTM used in the [previous notebook](4.sentence-level-prediction.ipynb). For each sentence we grouped them by their disease-gene mention and report their marginal probabilites in different quantiles (0, 0.2, 0.4, 0.6, 0.8). Lastly we took the average of each sentence marginal to generate the "avg_marginal" column.

# In[ ]:


train_marginals_df = pd.read_csv("vanilla_lstm/lstm_disease_gene_holdout/full_data/lstm_train_full_marginals.csv")
dev_marginals_df = pd.read_csv("vanilla_lstm/lstm_disease_gene_holdout/full_data/lstm_dev_full_marginals.csv")
test_marginals_df = pd.read_csv("vanilla_lstm/lstm_disease_gene_holdout/full_data/lstm_test_full_marginals.csv")


# In[ ]:


train_sentences_df = pd.read_csv("stratified_data/lstm_disease_gene_holdout/train_candidates_sentences.csv")
dev_sentences_df = pd.read_csv("stratified_data/lstm_disease_gene_holdout/dev_candidates_sentences.csv")
test_sentences_df = pd.read_csv("stratified_data/lstm_disease_gene_holdout/test_candidates_sentences.csv")


# In[ ]:


train_sentences_df["marginals"] = train_marginals_df["RNN_marginals"].values
dev_sentences_df["marginals"] = dev_marginals_df["RNN_marginals"].values
test_sentences_df["marginals"] = test_marginals_df["RNN_marginals"].values


# In[ ]:


candidate_marginals = (
    train_sentences_df[["disease_id", "gene_id", "marginals"]]
    .append(dev_sentences_df[["disease_id", "gene_id", "marginals"]])
    .append(test_sentences_df[["disease_id", "gene_id", "marginals"]])
    )


# In[ ]:


dg_map = pd.read_csv("dg_map.csv").rename(
    columns={"disease_ontology":"disease_id"},index=str)


# In[ ]:


quantile_list = [0,0.2,0.4,0.6,0.8]
quantile_data = []
avg_marginals = []
group = candidate_marginals.groupby(["disease_id", "gene_id"])

for i, cand in tqdm.tqdm(candidate_df[["disease_id", "gene_id"]].iterrows()):
    dg_series = group.get_group((cand["disease_id"], cand["gene_id"]))
    avg_marginals.append(dg_series["marginals"].mean())
    quantile_data.append(list(map(lambda x: dg_series["marginals"].quantile(x), quantile_list)))

# Save the evidence into a dataframe
candidate_df = pd.concat(
    [
        candidate_df,
        pd.DataFrame(
            quantile_data,
            index=candidate_df.index,
            columns=list(map(lambda x: 'lstm_marginal_{:.0f}_quantile'.format(x*100), quantile_list))
        )
    ], axis=1
)
candidate_df = pd.merge(candidate_df, 
                        dg_map[["disease_id", "disease_name", "gene_id", "gene_name"]],
                        on=["disease_id", "gene_id"], how="inner"
                       ).drop_duplicates(["disease_id", "gene_id"])
candidate_df["lstm_avg_marginal"] = avg_marginals


# ## Save the data to a file

# In[ ]:


candidate_df.to_csv("data/disease_gene_summary_stats.csv", index=False)

