import os
import pickle
import random
import json
import glob

from deepsnap.graph import Graph as DSGraph
from deepsnap.batch import Batch
from deepsnap.dataset import GraphDataset, Generator
import networkx as nx
import numpy as np
from sklearn.manifold import TSNE
import torch
import torch.multiprocessing as mp
import torch.nn.functional as F
import torch.optim as optim
from torch_geometric.data import DataLoader
from torch.utils.data import DataLoader as TorchDataLoader
from torch_geometric.datasets import TUDataset, PPI, QM9, WordNet18
import torch_geometric.utils as pyg_utils
import torch_geometric.nn as pyg_nn
from tqdm import tqdm
import queue
import scipy.stats as stats
from sklearn.preprocessing import OneHotEncoder

#from ogb.nodeproppred import PygNodePropPredDataset, Evaluator

from common import combined_syn
from common import feature_preprocess
from common import utils
from common.random_basis_dataset import RandomBasisDataset, toGT

def read_WN(path='data/WN18.gpickle'):
    graph = nx.read_gpickle(path).to_undirected()
    new_graph = nx.line_graph(graph)
    print(nx.number_of_nodes(new_graph))
    return [create_linegraph_features(graph, new_graph, 18)]

def read_ppi(dataset_path='data', dataset_str='ppi'):
    graph_json = json.load(open('{}/{}/{}-G.json'.format(dataset_path,
        dataset_str, dataset_str)))
    graph_nx = nx.readwrite.json_graph.node_link_graph(graph_json)
    graphs = create_features([graph_nx], 'const')
    print(nx.number_of_nodes(graph_nx))
    return graphs

def load_dataset(name, get_feats=False):
    """ Load real-world datasets, available in PyTorch Geometric.

    Used as a helper for DiskDataSource.
    """
    task = "graph"
    if name == "enzymes":
        dataset = TUDataset(root="/tmp/ENZYMES", name="ENZYMES")
    elif name == "proteins":
        dataset = TUDataset(root="/tmp/PROTEINS", name="PROTEINS")
    elif name == "cox2":
        dataset = TUDataset(root="/tmp/cox2", name="COX2")
    elif name == "dd":
        dataset = TUDataset(root="/tmp/DD", name="DD")
    elif name == "msrc":
        dataset = TUDataset(root="/tmp/MSRC_21", name="MSRC_21")
    elif name == "mmdb":
        dataset = TUDataset(root="/tmp/FIRSTMM_DB", name="FIRSTMM_DB")
    elif name == "aids":
        dataset = TUDataset(root="/tmp/AIDS", name="AIDS")
    elif name == "reddit-binary":
        dataset = TUDataset(root="/tmp/REDDIT-BINARY", name="REDDIT-BINARY")
        get_feats = False  # no feats
    elif name == "imdb-binary" or name == "imdb_binary":
        dataset = TUDataset(root="/tmp/IMDB-BINARY", name="IMDB-BINARY")
        get_feats = False  # no feats
    elif name == "firstmm_db":
        dataset = TUDataset(root="/tmp/FIRSTMM_DB", name="FIRSTMM_DB")
    elif name == "dblp":
        dataset = TUDataset(root="/tmp/DBLP_v1", name="DBLP_v1")
    elif name == "ppi":
        dataset = PPI(root="/tmp/PPI")
    elif name == "WN":
        dataset = WordNet18(root="/tmp/WN18")
        get_feats = False  # no feats
    elif name == "qm9":
        dataset = QM9(root="/tmp/QM9")
    elif name == "atlas":
        dataset = [g for g in nx.graph_atlas_g()[1:] if nx.is_connected(g)]
    elif name == "arxiv":
        dataset = PygNodePropPredDataset(name = "ogbn-arxiv")
    elif name == 'newdataset':
        dataset = get_newdataset("data/revit_graphs") #TODO: move path upwards
    if task == "graph":
        train_len = int(0.8 * len(dataset))
        train, test = [], []
        dataset = list(dataset)
        random.shuffle(dataset)
        has_name = hasattr(dataset[0], "name")
        for i, graph in tqdm(enumerate(dataset)):
            if not type(graph) == nx.Graph:
                if has_name: del graph.name
                if get_feats:
                    graph = pyg_utils.to_networkx(graph,
                        node_attrs=["x"]).to_undirected()
                    for v in graph:
                        graph.nodes[v]["feat"] = graph.nodes[v]["x"]
                else:
                    graph = pyg_utils.to_networkx(graph).to_undirected()
                    for v in graph:
                        graph.nodes[v]["feat"] = np.array([1])
            if i < train_len:
                train.append(graph)
            else:
                test.append(graph)
        if len(dataset) == 1:    # don't split graphs if single large graph
            train = test
    return train, test, task

class DataSource:
    def gen_batch(batch_target, batch_neg_target, batch_neg_query, train):
        raise NotImplementedError

class OTFSynDataSource(DataSource):
    """ On-the-fly generated synthetic data for training the subgraph model.

    At every iteration, new batch of graphs (positive and negative) are generated
    with a pre-defined generator (see combined_syn.py).

    DeepSNAP transforms are used to generate the positive and negative examples.
    """
    def __init__(self, max_size=29, min_size=5, n_workers=4,
        max_queue_size=256, node_anchored=False):
        self.closed = False
        self.max_size = max_size
        self.min_size = min_size
        self.node_anchored = node_anchored
        self.generator = combined_syn.get_generator(np.arange(
            self.min_size + 1, self.max_size + 1))

    def gen_data_loaders(self, size, batch_size, train=True,
        use_distributed_sampling=False):
        loaders = []
        for i in range(2):
            dataset = combined_syn.get_dataset("graph", size // 2,
                np.arange(self.min_size + 1, self.max_size + 1))
            sampler = torch.utils.data.distributed.DistributedSampler(
                dataset, num_replicas=hvd.size(), rank=hvd.rank()) if \
                    use_distributed_sampling else None
            loaders.append(TorchDataLoader(dataset,
                collate_fn=Batch.collate([]), batch_size=batch_size // 2 if i
                == 0 else batch_size // 2,
                sampler=sampler, shuffle=False))
        loaders.append([None]*(size // batch_size))
        return loaders

    def gen_batch(self, batch_target, batch_neg_target, batch_neg_query,
        train, epoch=None):
        def sample_subgraph(graph, offset=0, use_precomp_sizes=False,
            filter_negs=False, supersample_small_graphs=False, neg_target=None,
            hard_neg_idxs=None):
            if neg_target is not None: graph_idx = graph.G.graph["idx"]
            use_hard_neg = (hard_neg_idxs is not None and graph.G.graph["idx"]
                in hard_neg_idxs)
            done = False
            n_tries = 0
            while not done:
                if use_precomp_sizes:
                    size = graph.G.graph["subgraph_size"]
                else:
                    if train and supersample_small_graphs:
                        sizes = np.arange(self.min_size + offset,
                            len(graph.G) + offset)
                        ps = (sizes - self.min_size + 2) ** (-1.1)
                        ps /= ps.sum()
                        size = stats.rv_discrete(values=(sizes, ps)).rvs()
                    else:
                        d = 1 if train else 0
                        size = random.randint(self.min_size + offset - d,
                            len(graph.G) - 1 + offset)
                start_node = random.choice(list(graph.G.nodes))
                neigh = [start_node]
                frontier = list(set(graph.G.neighbors(start_node)) - set(neigh))
                visited = set([start_node])
                while len(neigh) < size:
                    new_node = random.choice(list(frontier))
                    assert new_node not in neigh
                    neigh.append(new_node)
                    visited.add(new_node)
                    frontier += list(graph.G.neighbors(new_node))
                    frontier = [x for x in frontier if x not in visited]
                if self.node_anchored:
                    anchor = neigh[0]
                    for v in graph.G.nodes:
                        graph.G.nodes[v]["node_feature"] = (torch.ones(1) if
                            anchor == v else torch.zeros(1))
                        #print(v, graph.G.nodes[v]["node_feature"])
                neigh = graph.G.subgraph(neigh)
                if use_hard_neg and train:
                    neigh = neigh.copy()
                    if random.random() < 1.0 or not self.node_anchored: # add edges
                        non_edges = list(nx.non_edges(neigh))
                        if len(non_edges) > 0:
                            for u, v in random.sample(non_edges, random.randint(1,
                                min(len(non_edges), 5))):
                                neigh.add_edge(u, v)
                    else:                         # perturb anchor
                        anchor = random.choice(list(neigh.nodes))
                        for v in neigh.nodes:
                            neigh.nodes[v]["node_feature"] = (torch.ones(1) if
                                anchor == v else torch.zeros(1))
                    matcher = nx.algorithms.isomorphism.GraphMatcher(
                        graph.G, neigh)

                if (filter_negs and train and len(neigh) <= 6 and neg_target is
                    not None):
                    matcher = nx.algorithms.isomorphism.GraphMatcher(
                        neg_target[graph_idx], neigh)
                    if not matcher.subgraph_is_isomorphic(): done = True
                else:
                    done = True

            return graph, DSGraph(neigh)

        augmenter = feature_preprocess.FeatureAugment()

        pos_target = batch_target
        pos_target, pos_query = pos_target.apply_transform_multi(sample_subgraph)
        neg_target = batch_neg_target
        # TODO: use hard negs
        hard_neg_idxs = set(random.sample(range(len(neg_target.G)),
            int(len(neg_target.G) * 1/2)))
        #hard_neg_idxs = set()
        batch_neg_query = Batch.from_data_list(
            [DSGraph(self.generator.generate(size=len(g))
                if i not in hard_neg_idxs else g)
                for i, g in enumerate(neg_target.G)])
        for i, g in enumerate(batch_neg_query.G):
            g.graph["idx"] = i
        _, neg_query = batch_neg_query.apply_transform_multi(sample_subgraph,
            hard_neg_idxs=hard_neg_idxs)
        if self.node_anchored:
            def add_anchor(g, anchors=None):
                if anchors is not None:
                    anchor = anchors[g.G.graph["idx"]]
                else:
                    anchor = random.choice(list(g.G.nodes))
                for v in g.G.nodes:
                    if "node_feature" not in g.G.nodes[v]:
                        g.G.nodes[v]["node_feature"] = (torch.ones(1) if anchor == v
                            else torch.zeros(1))
                return g
            neg_target = neg_target.apply_transform(add_anchor)
        pos_target = augmenter.augment(pos_target).to(utils.get_device())
        pos_query = augmenter.augment(pos_query).to(utils.get_device())
        neg_target = augmenter.augment(neg_target).to(utils.get_device())
        neg_query = augmenter.augment(neg_query).to(utils.get_device())
        #print(len(pos_target.G[0]), len(pos_query.G[0]))
        return pos_target, pos_query, neg_target, neg_query

class OTFSynImbalancedDataSource(OTFSynDataSource):
    """ Imbalanced on-the-fly synthetic data.

    Unlike the balanced dataset, this data source does not use 1:1 ratio for
    positive and negative examples. Instead, it randomly samples 2 graphs from
    the on-the-fly generator, and records the groundtruth label for the pair (subgraph or not).
    As a result, the data is imbalanced (subgraph relationships are rarer).
    This setting is a challenging model inference scenario.
    """
    def __init__(self, max_size=29, min_size=5, n_workers=4,
        max_queue_size=256, node_anchored=False):
        super().__init__(max_size=max_size, min_size=min_size,
            n_workers=n_workers, node_anchored=node_anchored)
        self.batch_idx = 0

    def gen_batch(self, graphs_a, graphs_b, _, train):
        def add_anchor(g):
            anchor = random.choice(list(g.G.nodes))
            for v in g.G.nodes:
                g.G.nodes[v]["node_feature"] = (torch.ones(1) if anchor == v
                    or not self.node_anchored else torch.zeros(1))
            return g
        pos_a, pos_b, neg_a, neg_b = [], [], [], []
        fn = "data/cache/imbalanced-{}-{}".format(str(self.node_anchored),
            self.batch_idx)
        if not os.path.exists(fn):
            graphs_a = graphs_a.apply_transform(add_anchor)
            graphs_b = graphs_b.apply_transform(add_anchor)
            for graph_a, graph_b in tqdm(list(zip(graphs_a.G, graphs_b.G))):
                matcher = nx.algorithms.isomorphism.GraphMatcher(graph_a, graph_b,
                    node_match=(lambda a, b: (a["node_feature"][0] > 0.5) ==
                    (b["node_feature"][0] > 0.5)) if self.node_anchored else None)
                if matcher.subgraph_is_isomorphic():
                    pos_a.append(graph_a)
                    pos_b.append(graph_b)
                else:
                    neg_a.append(graph_a)
                    neg_b.append(graph_b)
            if not os.path.exists("data/cache"):
                os.makedirs("data/cache")
            with open(fn, "wb") as f:
                pickle.dump((pos_a, pos_b, neg_a, neg_b), f)
            print("saved", fn)
        else:
            with open(fn, "rb") as f:
                print("loaded", fn)
                pos_a, pos_b, neg_a, neg_b = pickle.load(f)
        print(len(pos_a), len(neg_a))
        if pos_a:
            pos_a = utils.batch_nx_graphs(pos_a)
            pos_b = utils.batch_nx_graphs(pos_b)
        neg_a = utils.batch_nx_graphs(neg_a)
        neg_b = utils.batch_nx_graphs(neg_b)
        self.batch_idx += 1
        return pos_a, pos_b, neg_a, neg_b

class RandomBasisDataSource(DataSource):
    def __init__(self, dataset_name, node_anchored=False, min_size=5,
        max_size=29, edge_induced=False):
        self.dataset = load_dataset(dataset_name, get_feats=True)
        self.edge_induced = edge_induced
        self.dataset_name = dataset_name
    
    def gen_data_loaders(self, size, batch_size, train=True,
        use_distributed_sampling=False):
        loaders = [[batch_size]*(size // batch_size) for i in range(3)]
        return loaders

    def gen_batch(self, a, b, c, train, max_size=15, min_size=5, seed=None,
        filter_negs=False, epoch=None):
        batch_size = a
        train_data, test_data, _ = self.dataset
        sample = self.dataset_name in ["WN", "ppi", "reddit-binary"]
        basis_dataset = RandomBasisDataset(train_data if train else test_data,
            batch_size, induced=not self.edge_induced,
            phase="all" if train else "center", sample_neighborhoods=sample)
        if epoch is not None:
            old_hops = basis_dataset.query_hops
            basis_dataset.set_query_hops(min(epoch, 4))
            if basis_dataset.query_hops != old_hops:
                print("USING", basis_dataset.query_hops, "HOPS")
        pos_a, pos_b, neg_a, neg_b = [], [], [], []
        pos_a_anchors, pos_b_anchors, neg_a_anchors, neg_b_anchors = [], [], [], []
        for (query_adj, query_feat, center, neighborhood_adj, neighborhood_feat,
            neighborhood_center, label, idx) in basis_dataset:
            if label == 1:
                pos_a.append(toGT(neighborhood_adj[0], neighborhood_feat))
                pos_b.append(toGT(query_adj[0], query_feat))
                pos_a_anchors.append(neighborhood_center)
                pos_b_anchors.append(center)
            else:
                neg_a.append(toGT(neighborhood_adj[0], neighborhood_feat))
                neg_b.append(toGT(query_adj[0], query_feat))
                neg_a_anchors.append(neighborhood_center)
                neg_b_anchors.append(center)

        if len(pos_a) > 0:
            pos_a = utils.batch_nx_graphs(pos_a, anchors=pos_a_anchors)
            pos_b = utils.batch_nx_graphs(pos_b, anchors=pos_b_anchors)
        if len(neg_a) > 0:
            neg_a = utils.batch_nx_graphs(neg_a, anchors=neg_a_anchors)
            neg_b = utils.batch_nx_graphs(neg_b, anchors=neg_b_anchors)
        return pos_a, pos_b, neg_a, neg_b

class DiskDataSource(DataSource):
    """ Uses a set of graphs saved in a dataset file to train the subgraph model.

    At every iteration, new batch of graphs (positive and negative) are generated
    by sampling subgraphs from a given dataset.

    See the load_dataset function for supported datasets.
    """
    def __init__(self, dataset_name, node_anchored=False, min_size=5,
        max_size=50, use_feats=False, sampling_method="tree-pair"):
        self.node_anchored = node_anchored
        self.dataset = (load_dataset(dataset_name, get_feats=use_feats)
            if dataset_name != "syn" else None)
        #self.dataset = None
        self.min_size = min_size
        self.max_size = max_size
        self.use_feats = use_feats
        self.sampling_method = sampling_method

    def gen_data_loaders(self, size, batch_size, train=True,
        use_distributed_sampling=False):
        loaders = [[batch_size]*(size // batch_size) for i in range(3)]
        return loaders

    def gen_batch(self, a, b, c, train, max_size=50, min_size=5, seed=None,
        filter_negs=False, sample_method="tree-pair", epoch=None):
        
        sample_method = self.sampling_method
        batch_size = a
        train_set, test_set, task = self.dataset
        graphs = train_set if train else test_set
        if seed is not None:
            random.seed(seed)

        pos_a, pos_b = [], []
        pos_a_anchors, pos_b_anchors = [], []
        for i in range(batch_size // 2):
            if sample_method in ["tree-pair", "random-walks"]:
                size = random.randint(min_size+1, max_size)
                graph, a = utils.sample_neigh(graphs, size,
                    method=sample_method)
                b = a[:random.randint(min_size, len(a) - 1)]
            elif sample_method == "subgraph-tree":
                graph = None
                while graph is None or len(graph) < min_size + 1:
                    graph = random.choice(graphs)
                a = graph.nodes
                _, b = utils.sample_neigh([graph], random.randint(min_size,
                    len(graph) - 1))
            if self.node_anchored:
                anchor = list(graph.nodes)[0]
                pos_a_anchors.append(anchor)
                pos_b_anchors.append(anchor)
            neigh_a, neigh_b = graph.subgraph(a), graph.subgraph(b)
            pos_a.append(neigh_a)
            pos_b.append(neigh_b)

        neg_a, neg_b = [], []
        neg_a_anchors, neg_b_anchors = [], []
        while len(neg_a) < batch_size // 2:
            if sample_method in ["tree-pair", "random-walks"]:
                size = random.randint(min_size+1, max_size)
                graph_a, a = utils.sample_neigh(graphs, size,
                    method=sample_method)
                graph_b, b = utils.sample_neigh(graphs, random.randint(min_size,
                    size - 1), method=sample_method)
            elif sample_method == "subgraph-tree":
                graph_a = None
                while graph_a is None or len(graph_a) < min_size + 1:
                    graph_a = random.choice(graphs)
                a = graph_a.nodes
                graph_b, b = utils.sample_neigh(graphs, random.randint(min_size,
                    len(graph_a) - 1))
            if self.node_anchored:
                neg_a_anchors.append(list(graph_a.nodes)[0])
                neg_b_anchors.append(list(graph_b.nodes)[0])
            neigh_a, neigh_b = graph_a.subgraph(a), graph_b.subgraph(b)
            if filter_negs:
                matcher = nx.algorithms.isomorphism.GraphMatcher(neigh_a, neigh_b)
                if matcher.subgraph_is_isomorphic(): # a <= b (b is subgraph of a)
                    continue
            neg_a.append(neigh_a)
            neg_b.append(neigh_b)

        pos_a = utils.batch_nx_graphs(pos_a, anchors=pos_a_anchors if
            self.node_anchored else None)
        pos_b = utils.batch_nx_graphs(pos_b, anchors=pos_b_anchors if
            self.node_anchored else None)
        neg_a = utils.batch_nx_graphs(neg_a, anchors=neg_a_anchors if
            self.node_anchored else None)
        neg_b = utils.batch_nx_graphs(neg_b, anchors=neg_b_anchors if
            self.node_anchored else None)
        return pos_a, pos_b, neg_a, neg_b


def get_newdataset(path):
    
    def get_feature(dict, feature_path):
      paths = feature_path.split('.')
      feature = dict
      for path in paths:
        feature = feature[path]
      return feature

    def get_features(dict, features_paths):
      features = []
      for path in features_paths:
        feature = get_feature(dict, path)
        features.append(feature)
      return features
    
    def extract_graph_properties(graph):
        nodes_names = []
        nodes_features = []
        edges = {}
        features_paths = ['grgentype', 'misc.properties.type'] #TODO: write in a file

        for key, val in graph.items():
          if key[0] != 'e': # a node
            nodes_names.append(key)
            features = get_features(val, features_paths)
            nodes_features.append(features) 
          else: # an edge
            edges[key] = val

        return nodes_names, nodes_features, edges

    data = []
    enc = OneHotEncoder(handle_unknown='ignore', sparse=False)

    files = glob.glob(path + '/*')
    for i, file in enumerate(files):

        graph = json.load(open(file))
        nodes_names, nodes_features, edges = extract_graph_properties(graph)
        if i == 0: # fit one hot encoder on the first sample which should contain same features as all others
            enc.fit(nodes_features)

        transformed_features = enc.transform(nodes_features)

        # build networkX graph
        G = nx.Graph()
        for name, features in zip(nodes_names, transformed_features):
          G.add_nodes_from([(name, {'feat': features})])

        for key, edge in edges.items():
          G.add_edges_from([(edge[1], edge[2])]) #, {'feat': edge[0]})]) #TODO: Add edge features
        data.append(G)    
    
    return data


class DiskImbalancedDataSource(OTFSynDataSource):
    """ Imbalanced on-the-fly real data.

    Unlike the balanced dataset, this data source does not use 1:1 ratio for
    positive and negative examples. Instead, it randomly samples 2 graphs from
    the on-the-fly generator, and records the groundtruth label for the pair (subgraph or not).
    As a result, the data is imbalanced (subgraph relationships are rarer).
    This setting is a challenging model inference scenario.
    """
    def __init__(self, dataset_name, max_size=29, min_size=5, n_workers=4,
        max_queue_size=256, node_anchored=False, use_whole_targets=False,
        use_feats=False, target_larger=True):
        super().__init__(max_size=max_size, min_size=min_size,
            n_workers=n_workers, node_anchored=node_anchored)
        self.batch_idx = 0
        self.dataset = (load_dataset(dataset_name, get_feats=use_feats)
            if dataset_name != "syn" else (None, None, None))
        self.train_set, self.test_set, _ = self.dataset
        self.dataset_name = dataset_name
        self.use_whole_targets = use_whole_targets
        self.use_feats = use_feats
        self.fn_prefix = "data/cache/imbalanced"
        self.use_precomputed_positives = False
        self.target_larger = target_larger

    def gen_data_loaders(self, size, batch_size, train=True,
        use_distributed_sampling=False):
        loaders = []
        for i in range(2):
            neighs = []
            for j in range(size // 2):
                if self.use_whole_targets and i == 0:
                    neighs.append(random.choice(self.train_set if train else
                        self.test_set))
                else:
                    graph, neigh = utils.sample_neigh(self.train_set if train else
                        self.test_set, random.randint(self.min_size, self.max_size))
                    neighs.append(graph.subgraph(neigh))
            dataset = GraphDataset(neighs)
            loaders.append(TorchDataLoader(dataset,
                collate_fn=Batch.collate([]), batch_size=batch_size // 2 if i
                == 0 else batch_size // 2,
                sampler=None, shuffle=False))
        loaders.append([None]*(size // batch_size))
        return loaders

    def gen_batch(self, graphs_a, graphs_b, _, train):
        def add_anchor(g):
            anchor = random.choice(list(g.G.nodes))
            for v in g.G.nodes:
                g.G.nodes[v]["node_feature"] = (torch.ones(1) if anchor == v
                    or not self.node_anchored else torch.zeros(1))
            return g
        pos_a, pos_b, neg_a, neg_b = [], [], [], []
        fn = "{}-{}-{}-{}-{}-{}-{}".format(self.fn_prefix,
            self.dataset_name.lower(),
            str(self.node_anchored), str(self.use_whole_targets),
            str(self.use_feats), str(self.target_larger),
            self.batch_idx)
        if not os.path.exists(fn):
            print("making new data")
            graphs_a = graphs_a.apply_transform(add_anchor)
            graphs_b = graphs_b.apply_transform(add_anchor)
            for i, (graph_a, graph_b) in tqdm(enumerate(list(zip(graphs_a.G,
                graphs_b.G)))):
                if self.target_larger:
                    if len(graph_a) < len(graph_b):
                        graph_a, graph_b = graph_b, graph_a
                matcher = nx.algorithms.isomorphism.GraphMatcher(graph_a, graph_b,
                    node_match=(lambda a, b: (a["node_feature"][0] > 0.5) ==
                    (b["node_feature"][0] > 0.5)) if self.node_anchored else None)
                if ((self.use_precomputed_positives and i % 2 == 0) or
                    matcher.subgraph_is_isomorphic()):
                    pos_a.append(graph_a)
                    pos_b.append(graph_b)
                else:
                    neg_a.append(graph_a)
                    neg_b.append(graph_b)
            if not os.path.exists("data/cache"):
                os.makedirs("data/cache")
            with open(fn, "wb") as f:
                pickle.dump((pos_a, pos_b, neg_a, neg_b), f)
            print("saved", fn)
        else:
            print("loading data")
            with open(fn, "rb") as f:
                print("loaded", fn)
                pos_a, pos_b, neg_a, neg_b = pickle.load(f)
        print(len(pos_a), len(neg_a))
        if pos_a:
            pos_a = utils.batch_nx_graphs(pos_a)
            pos_b = utils.batch_nx_graphs(pos_b)
        neg_a = utils.batch_nx_graphs(neg_a)
        neg_b = utils.batch_nx_graphs(neg_b)
        self.batch_idx += 1
        return pos_a, pos_b, neg_a, neg_b

class PerturbTargetDataSource(DiskImbalancedDataSource):
    def gen_data_loaders(self, size, batch_size, train=True,
        use_distributed_sampling=False):
        #print("PERTURB")
        self.fn_prefix = "data/cache/imbalanced-perturb"
        self.max_size = 40
        self.use_precomputed_positives = True
        all_neighs = []
        self.generator = combined_syn.get_generator(np.arange(
            self.min_size + 1, self.max_size + 1))

        for i in range(2):
            neighs = []
            for j in range(size // 2):
                if i == 0:
                    if self.use_whole_targets:
                        found = False
                        while not found:
                            neigh = random.choice(self.train_set if train else
                                self.test_set)
                            if len(neigh) > 5:
                                found = True
                        neighs.append(neigh)
                    else:
                        if self.dataset_name == "syn":
                            neighs.append(self.generator.generate(size=random.randint(
                                20, self.max_size)))
                        else:
                            graph, neigh = utils.sample_neigh(self.train_set if
                                train else self.test_set, random.randint(
                                    20, self.max_size))
                            neighs.append(graph.subgraph(neigh))
                else:
                    if self.use_whole_targets:
                        graph, neigh = utils.sample_neigh(self.train_set if
                            train else self.test_set, random.randint(
                                self.min_size, min(self.max_size, len(all_neighs[0][j]))))
                        neighs.append(graph.subgraph(neigh))
                        if i == 1 and j % 2 == 0:
                            all_neighs[0][j] = graph
                    else:
                        if self.dataset_name == "syn":
                            tgt_graph = self.generator.generate(size=len(
                                all_neighs[0][j]))
                        else:
                            tgt_graph, tgt = utils.sample_neigh(self.train_set if
                                train else self.test_set, len(all_neighs[0][j]))
                            tgt_graph = tgt_graph.subgraph(tgt)
                        qry_graph, qry = utils.sample_neigh([tgt_graph],
                            random.randint(self.min_size, len(tgt_graph)))
                            #random.randint(self.min_size, 20))
                        neighs.append(qry_graph.subgraph(qry))
                        if i == 1 and j % 2 == 0:
                            all_neighs[0][j] = tgt_graph

            all_neighs.append(neighs)
        loaders = []
        for i in range(2):
            dataset = GraphDataset(GraphDataset.list_to_graphs(all_neighs[i]))
            loaders.append(TorchDataLoader(dataset,
                collate_fn=Batch.collate([]), batch_size=batch_size // 2 if i
                == 0 else batch_size // 2,
                sampler=None, shuffle=False))
        loaders.append([None]*(size // batch_size))
        return loaders

if __name__ == "__main__":
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 14})
    for name in ["enzymes", "reddit-binary", "cox2"]:
        data_source = DiskDataSource(name)
        train, test, _ = data_source.dataset
        i = 11
        neighs = [utils.sample_neigh(train, i) for j in range(10000)]
        clustering = [nx.average_clustering(graph.subgraph(nodes)) for graph,
            nodes in neighs]
        path_length = [nx.average_shortest_path_length(graph.subgraph(nodes))
            for graph, nodes in neighs]
        #plt.subplot(1, 2, i-9)
        plt.scatter(clustering, path_length, s=10, label=name)
    plt.legend()
    plt.savefig("plots/clustering-vs-path-length.png")
