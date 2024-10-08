from typing import Any, Callable, List, Tuple, Union, Dict, Sequence

from math import sqrt
from unicodedata import name
import scipy as sp

import torch
from torch import Tensor
from torch.nn import Module
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
import copy
import matplotlib.pyplot as plt
import networkx as nx
from torch_geometric.nn import MessagePassing
from torch_geometric.utils.loop import add_self_loops, remove_self_loops
from torch_geometric.data import Data, Batch
from torch_geometric.utils import to_networkx
from benchmark.models.utils import subgraph, normalize
from torch.nn.functional import binary_cross_entropy as bceloss
from typing_extensions import Literal
from benchmark.kernel.utils import Metric
from benchmark.data.dataset import data_args
from benchmark.args import x_args
from rdkit import Chem
from matplotlib.axes import Axes
from matplotlib.patches import Path, PathPatch
import json,glob,os


import captum
import captum.attr as ca
from captum.attr._utils.typing import (
    BaselineType,
    Literal,
    TargetType,
    TensorOrTupleOfTensorsGeneric,
)
from captum.attr._core.deep_lift import DeepLiftShap
from captum.attr._utils.attribution import GradientAttribution, LayerAttribution
from captum.attr._utils.common import (
    ExpansionTypes,
    _call_custom_attribution_func,
    _compute_conv_delta_and_format_attrs,
    _expand_additional_forward_args,
    _expand_target,
    _format_additional_forward_args,
    _format_attributions,
    _format_baseline,
    _format_callable_baseline,
    _format_input,
    _tensorize_baseline,
    _validate_input,
)
from captum.attr._utils.gradient import (
    apply_gradient_requirements,
    compute_layer_gradients_and_eval,
    undo_gradient_requirements,
)
import benchmark.models.gradient_utils as gu

from itertools import combinations
import numpy as np
from benchmark.models.models import GlobalMeanPool, GraphSequential, GNNPool
from benchmark.models.ext.deeplift.layer_deep_lift import LayerDeepLift, DeepLift
import shap
import time

from dgl import DGLGraph

from torch.nn import BCELoss


EPS = 1e-15

class Pair():
    def __init__(self, index, score):
        self.index = index
        self.score = score
    
    def __lt__(self, other):
        if self.score <= other.score :
            return True
        else:
            return False


class ExplainerBase(nn.Module):

    def __init__(self, model: nn.Module, epochs=0, lr=0, explain_graph=False, molecule=False):
        super().__init__()
        self.model = model
        self.lr = lr
        self.epochs = epochs
        self.explain_graph = explain_graph
        self.molecule = molecule
        self.mp_layers = [module for module in self.model.modules() if isinstance(module, MessagePassing)]
        self.num_layers = len(self.mp_layers)

        self.ori_pred = None
        self.ex_labels = None
        self.edge_mask = None
        self.hard_edge_mask = None

        self.num_edges = None
        self.num_nodes = None
        self.device = None
        self.table = Chem.GetPeriodicTable().GetElementSymbol

    def __set_masks__(self, x, edge_index, init="normal"):
        (N, F), E = x.size(), edge_index.size(1)

        std = 0.1
        self.node_feat_mask = torch.nn.Parameter(torch.randn(F, requires_grad=True, device=self.device) * 0.1)

        std = torch.nn.init.calculate_gain('relu') * sqrt(2.0 / (2 * N))
        self.edge_mask = torch.nn.Parameter(torch.randn(E, requires_grad=True, device=self.device) * std)
        # self.edge_mask = torch.nn.Parameter(100 * torch.ones(E, requires_grad=True))

        for module in self.model.modules():
            if isinstance(module, MessagePassing):
                module.__explain__ = True
                module.__edge_mask__ = self.edge_mask

    def __clear_masks__(self):
        for module in self.model.modules():
            if isinstance(module, MessagePassing):
                module.__explain__ = False
                module.__edge_mask__ = None
        self.node_feat_masks = None
        self.edge_mask = None

    @property
    def __num_hops__(self):
        if self.explain_graph:
            return -1
        else:
            return self.num_layers

    def __flow__(self):
        for module in self.model.modules():
            if isinstance(module, MessagePassing):
                return module.flow
        return 'source_to_target'

    def __subgraph__(self, node_idx, x, edge_index, **kwargs):
        num_nodes, num_edges = x.size(0), edge_index.size(1)

        subset, edge_index, mapping, edge_mask = subgraph(
            node_idx, self.__num_hops__, edge_index, relabel_nodes=True,
            num_nodes=num_nodes, flow=self.__flow__())

        x = x[subset]
        for key, item in kwargs.items():
            if torch.is_tensor(item) and item.size(0) == num_nodes:
                item = item[subset]
            elif torch.is_tensor(item) and item.size(0) == num_edges:
                item = item[edge_mask]
            kwargs[key] = item

        return x, edge_index, mapping, edge_mask, kwargs


    def forward(self,
                x: Tensor,
                edge_index: Tensor,
                **kwargs
                ):
        self.num_edges = edge_index.shape[1]
        self.num_nodes = x.shape[0]
        self.device = x.device


    def control_sparsity(self, edge_index, edge_mask, name, sparsity=None):
    #def control_sparsity(self, mask, sparsity=None):
        r"""

        :param mask: mask that need to transform
        :param sparsity: sparsity we need to control i.e. 0.7, 0.5
        :return: transformed mask where top 1 - sparsity values are set to inf.
        """

        
        if sparsity is None:
            sparsity = 0.8

#         node_mask = {}
#         for i in range(len(edge_index[0])):
#             if int(edge_index[0][i]) not in node_mask:
#                 node_mask[int(edge_index[0][i])] = float(edge_mask[i])
#             else:
#                 node_mask[int(edge_index[0][i])] += float(edge_mask[i])
#             if int(edge_index[1][i]) not in node_mask:
#                 node_mask[int(edge_index[1][i])] = float(edge_mask[i])
#             else:
#                 node_mask[int(edge_index[1][i])] += float(edge_mask[i])
# #
#         sorted_node_mask = sorted(node_mask.items(), key = lambda x: x[1], reverse = True)
        # return sorted_node_mask
        sorted_node_in = []
        sorted_node_out = []
        node_dict={}
        _, indices = torch.sort(edge_mask, descending=True)
        for index, i in enumerate(indices):
            if int(edge_index[0][i]) not in sorted_node_in:
                sorted_node_in.append(int(edge_index[0][i]))
            if int(edge_index[1][i]) not in sorted_node_in and int(edge_index[1][i]) not in sorted_node_out:
                sorted_node_out.append(int(edge_index[1][i]))
            if int(edge_index[0][i]) not in node_dict:
                node_dict[int(edge_index[0][i])] = index
            if int(edge_index[1][i]) not in node_dict:
                node_dict[int(edge_index[1][i])] = index
        return sorted_node_in, sorted_node_out, node_dict
        
        sorted_indices = [node[0] for node in sorted_node_mask]
#
        important_indices_length = int((1 - sparsity) * len(sorted_indices))
        number = 0
        important_indices = []
        for indice in sorted_indices:
            #if node_mask[indice] == 0:
            #    continue
            if number > important_indices_length:
                break
            important_indices.append(indice)
            number += 1
#
        out_path = '/home/DIG-main/dig/xgraph/GNNExplainer-master/reveal/' + name[0]
        with open(out_path, 'w') as wp:
            #wp.write('node length: ' + str(x_len) + ' \n')
            #wp.write('important node length: ' + str(len(important_indices)) + ' \n')
            json.dump(important_indices, wp)

    def visualize_graph(self, node_idx, edge_index, edge_mask, y=None, name =None,
                           threshold=None, **kwargs) -> Tuple[Axes, nx.DiGraph]:
        r"""Visualizes the subgraph around :attr:`node_idx` given an edge mask
        :attr:`edge_mask`.

        Args:
            node_idx (int): The node id to explain.
            edge_index (LongTensor): The edge indices.
            edge_mask (Tensor): The edge mask.
            y (Tensor, optional): The ground-truth node-prediction labels used
                as node colorings. (default: :obj:`None`)
            threshold (float, optional): Sets a threshold for visualizing
                important edges. If set to :obj:`None`, will visualize all
                edges with transparancy indicating the importance of edges.
                (default: :obj:`None`)
            **kwargs (optional): Additional arguments passed to
                :func:`nx.draw`.

        :rtype: :class:`matplotlib.axes.Axes`, :class:`networkx.DiGraph`
        """
        edge_index, _ = add_self_loops(edge_index, num_nodes=kwargs.get('num_nodes'))
        assert edge_mask.size(0) == edge_index.size(1)

        if self.molecule:
            atomic_num = torch.clone(y)

        # Only operate on a k-hop subgraph around `node_idx`.
        subset, edge_index, _, hard_edge_mask = subgraph(
            node_idx, self.__num_hops__, edge_index, relabel_nodes=True,
            num_nodes=None, flow=self.__flow__())
        
        edge_mask = edge_mask[hard_edge_mask]
        
        with open('/home/mytest/GNNExplainer-master/gnn_results/' + name[0], 'w') as wp:
            json.dump(subset.tolist(), wp)
        return

        # --- temp ---
        edge_mask[edge_mask == float('inf')] = 1
        edge_mask[edge_mask == - float('inf')] = 0
        # ---

        if threshold is not None:
            edge_mask = (edge_mask >= threshold).to(torch.float)

        if data_args.dataset_name == 'ba_lrp':
            y = torch.zeros(edge_index.max().item() + 1,
                            device=edge_index.device)
        if y is None:
            y = torch.zeros(edge_index.max().item() + 1,
                            device=edge_index.device)
        else:
            y = y[subset]

        if self.molecule:
            atom_colors = {6: '#8c69c5', 7: '#71bcf0', 8: '#aef5f1', 9: '#bdc499', 15: '#c22f72', 16: '#f3ea19',
                           17: '#bdc499', 35: '#cc7161'}
            node_colors = [None for _ in range(y.shape[0])]
            for y_idx in range(y.shape[0]):
                node_colors[y_idx] = atom_colors[y[y_idx].int().tolist()]
        else:
            atom_colors = {0: '#8c69c5', 1: '#c56973', 2: '#a1c569', 3: '#69c5ba'}
            node_colors = [None for _ in range(y.shape[0])]
            for y_idx in range(y.shape[0]):
                node_colors[y_idx] = atom_colors[y[y_idx].int().tolist()]


        data = Data(edge_index=edge_index, att=edge_mask, y=y, name=name,
                    num_nodes=y.size(0)).to('cpu')
        G = to_networkx(data, node_attrs=['y'], edge_attrs=['att'])
        mapping = {k: i for k, i in enumerate(subset.tolist())}
        G = nx.relabel_nodes(G, mapping)

        kwargs['with_labels'] = kwargs.get('with_labels') or True
        kwargs['font_size'] = kwargs.get('font_size') or 10
        kwargs['node_size'] = kwargs.get('node_size') or 250
        kwargs['cmap'] = kwargs.get('cmap') or 'cool'

        # calculate Graph positions
        pos = nx.kamada_kawai_layout(G)
        ax = plt.gca()

        for source, target, data in G.edges(data=True):
            ax.annotate(
                '', xy=pos[target], xycoords='data', xytext=pos[source],
                textcoords='data', arrowprops=dict(
                    arrowstyle="->",
                    lw=max(data['att'], 0.5) * 2,
                    alpha=max(data['att'], 0.4),  # alpha control transparency
                    color='#e1442a',  # color control color
                    shrinkA=sqrt(kwargs['node_size']) / 2.0,
                    shrinkB=sqrt(kwargs['node_size']) / 2.0,
                    connectionstyle="arc3,rad=0.08",  # rad control angle
                ))
        nx.draw_networkx_nodes(G, pos, node_color=node_colors, **kwargs)
        # define node labels
        if self.molecule:
            if x_args.nolabel:
                node_labels = {n: f'{self.table(atomic_num[n].int().item())}'
                               for n in G.nodes()}
                nx.draw_networkx_labels(G, pos, labels=node_labels, **kwargs)
            else:
                node_labels = {n: f'{n}:{self.table(atomic_num[n].int().item())}'
                               for n in G.nodes()}
                nx.draw_networkx_labels(G, pos, labels=node_labels, **kwargs)
        else:
            if not x_args.nolabel:
                nx.draw_networkx_labels(G, pos, **kwargs)

        return ax, G

    def visualize_walks(self, node_idx, edge_index, walks, edge_mask, y=None,
                        threshold=None, **kwargs) -> Tuple[Axes, nx.DiGraph]:
        r"""Visualizes the subgraph around :attr:`node_idx` given an edge mask
        :attr:`edge_mask`.

        Args:
            node_idx (int): The node id to explain.
            edge_index (LongTensor): The edge indices.
            edge_mask (Tensor): The edge mask.
            y (Tensor, optional): The ground-truth node-prediction labels used
                as node colorings. (default: :obj:`None`)
            threshold (float, optional): Sets a threshold for visualizing
                important edges. If set to :obj:`None`, will visualize all
                edges with transparancy indicating the importance of edges.
                (default: :obj:`None`)
            **kwargs (optional): Additional arguments passed to
                :func:`nx.draw`.

        :rtype: :class:`matplotlib.axes.Axes`, :class:`networkx.DiGraph`
        """
        self_loop_edge_index, _ = add_self_loops(edge_index, num_nodes=kwargs.get('num_nodes'))
        assert edge_mask.size(0) == self_loop_edge_index.size(1)

        if self.molecule:
            atomic_num = torch.clone(y)

        # Only operate on a k-hop subgraph around `node_idx`.
        subset, edge_index, _, hard_edge_mask = subgraph(
            node_idx, self.__num_hops__, self_loop_edge_index, relabel_nodes=True,
            num_nodes=None, flow=self.__flow__())

        edge_mask = edge_mask[hard_edge_mask]

        # --- temp ---
        edge_mask[edge_mask == float('inf')] = 1
        edge_mask[edge_mask == - float('inf')] = 0
        # ---

        if threshold is not None:
            edge_mask = (edge_mask >= threshold).to(torch.float)

        if data_args.dataset_name == 'ba_lrp':
            y = torch.zeros(edge_index.max().item() + 1,
                            device=edge_index.device)
        if y is None:
            y = torch.zeros(edge_index.max().item() + 1,
                            device=edge_index.device)
        else:
            y = y[subset]

        if self.molecule:
            atom_colors = {6: '#8c69c5', 7: '#71bcf0', 8: '#aef5f1', 9: '#bdc499', 15: '#c22f72', 16: '#f3ea19',
                           17: '#bdc499', 35: '#cc7161'}
            node_colors = [None for _ in range(y.shape[0])]
            for y_idx in range(y.shape[0]):
                node_colors[y_idx] = atom_colors[y[y_idx].int().tolist()]
        else:
            atom_colors = {0: '#8c69c5', 1: '#c56973', 2: '#a1c569', 3: '#69c5ba'}
            node_colors = [None for _ in range(y.shape[0])]
            for y_idx in range(y.shape[0]):
                node_colors[y_idx] = atom_colors[y[y_idx].int().tolist()]

        data = Data(edge_index=edge_index, att=edge_mask, y=y,
                    num_nodes=y.size(0)).to('cpu')
        G = to_networkx(data, node_attrs=['y'], edge_attrs=['att'])
        mapping = {k: i for k, i in enumerate(subset.tolist())}
        G = nx.relabel_nodes(G, mapping)

        kwargs['with_labels'] = kwargs.get('with_labels') or True
        kwargs['font_size'] = kwargs.get('font_size') or 8
        kwargs['node_size'] = kwargs.get('node_size') or 200
        kwargs['cmap'] = kwargs.get('cmap') or 'cool'

        # calculate Graph positions
        pos = nx.kamada_kawai_layout(G)
        ax = plt.gca()

        for source, target, data in G.edges(data=True):
            ax.annotate(
                '', xy=pos[target], xycoords='data', xytext=pos[source],
                textcoords='data', arrowprops=dict(
                    arrowstyle="-",
                    lw=1.5,
                    alpha=0.5,  # alpha control transparency
                    color='grey',  # color control color
                    shrinkA=sqrt(kwargs['node_size']) / 2.0,
                    shrinkB=sqrt(kwargs['node_size']) / 2.0,
                    connectionstyle="arc3,rad=0",  # rad control angle
                ))


        # --- try to draw a walk ---
        walks_ids = walks['ids']
        walks_score = walks['score']
        walks_node_list = []
        for i in range(walks_ids.shape[1]):
            if i == 0:
                walks_node_list.append(self_loop_edge_index[:, walks_ids[:, i].view(-1)].view(2, -1))
            else:
                walks_node_list.append(self_loop_edge_index[1, walks_ids[:, i].view(-1)].view(1, -1))
        walks_node_ids = torch.cat(walks_node_list, dim=0).T

        walks_mask = torch.zeros(walks_node_ids.shape, dtype=bool, device=self.device)
        for n in G.nodes():
            walks_mask = walks_mask | (walks_node_ids == n)
        walks_mask = walks_mask.sum(1) == walks_node_ids.shape[1]

        sub_walks_node_ids = walks_node_ids[walks_mask]
        sub_walks_score = walks_score[walks_mask]

        for i, walk in enumerate(sub_walks_node_ids):
            verts = [pos[n.item()] for n in walk]
            if walk.shape[0] == 3:
                codes = [Path.MOVETO, Path.CURVE3, Path.CURVE3]
            else:
                codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4]
            path = Path(verts, codes)
            if sub_walks_score[i] > 0:
                patch = PathPatch(path, facecolor='none', edgecolor='red', lw=1.5,#e1442a
                                  alpha=(sub_walks_score[i] / (sub_walks_score.max() * 2)).item())
            else:
                patch = PathPatch(path, facecolor='none', edgecolor='blue', lw=1.5,#18d66b
                                  alpha=(sub_walks_score[i] / (sub_walks_score.min() * 2)).item())
            ax.add_patch(patch)


        nx.draw_networkx_nodes(G, pos, node_color=node_colors, **kwargs)
        # define node labels
        if self.molecule:
            if x_args.nolabel:
                node_labels = {n: f'{self.table(atomic_num[n].int().item())}'
                               for n in G.nodes()}
                nx.draw_networkx_labels(G, pos, labels=node_labels, **kwargs)
            else:
                node_labels = {n: f'{n}:{self.table(atomic_num[n].int().item())}'
                               for n in G.nodes()}
                nx.draw_networkx_labels(G, pos, labels=node_labels, **kwargs)
        else:
            if not x_args.nolabel:
                nx.draw_networkx_labels(G, pos, **kwargs)

        return ax, G

    def type_conversion(self, x, edge_index, edge_attr):
        graph = DGLGraph()
        x=x.cpu()
        graph.add_nodes(len(x), data={'features': torch.FloatTensor(x)})
        edge_index_list = edge_index.cpu().t().numpy().tolist()
        edge_attr_list = edge_attr.cpu().numpy().tolist()
        for i in range(len(edge_index_list)):
            graph.add_edges(edge_index_list[i][0], edge_index_list[i][1], data={'etype': torch.LongTensor([edge_attr_list[i][0]])})

        return graph

    def eval_related_pred(self, x, edge_index, edge_attr, edge_masks, **kwargs):

        node_idx = kwargs.get('node_idx')
        node_idx = 0 if node_idx is None else node_idx  # graph level: 0, node level: node_idx
        related_preds = []

        for ex_label, edge_mask in enumerate(edge_masks):

            self.edge_mask.data = float('inf') * torch.ones(edge_mask.size(), device=data_args.device)
            graph = self.type_conversion(x, edge_index, edge_attr)
            ori_pred = self.model(graph, cuda=True)

            self.edge_mask.data = edge_mask
            graph = self.type_conversion(x, edge_index, edge_attr)
            masked_pred = self.model(graph, cuda=True)

            # mask out important elements for fidelity calculation
            self.edge_mask.data = - edge_mask  # keep Parameter's id
            graph = self.type_conversion(x, edge_index, edge_attr)
            maskout_pred = self.model(graph, cuda=True)

            # zero_mask
            self.edge_mask.data = - float('inf') * torch.ones(edge_mask.size(), device=data_args.device)
            graph = self.type_conversion(x, edge_index, edge_attr)
            zero_mask_pred = self.model(graph, cuda=True)

            related_preds.append({'zero': zero_mask_pred[node_idx],
                                  'masked': masked_pred[node_idx],
                                  'maskout': maskout_pred[node_idx],
                                  'origin': ori_pred[node_idx]})

            # Adding proper activation function to the models' outputs.
            if 'cs' in Metric.cur_task:
                related_preds[ex_label] = {key: pred.softmax(0)[ex_label].item()
                                        for key, pred in related_preds[ex_label].items()}

        return related_preds


class MyVulExplainer(ExplainerBase):
    """Args:
        model (torch.nn.Module): The GNN module to explain.
        epochs (int, optional): The number of epochs to train.
            (default: :obj:`100`)
        lr (float, optional): The learning rate to apply.
            (default: :obj:`0.01`)
        log (bool, optional): If set to :obj:`False`, will not log any learning
            progress. (default: :obj:`True`)
    """

    coeffs = {
        'edge_size': 0.005,
        'node_feat_size': 1.0,
        'edge_ent': 1.0,
        'node_feat_ent': 0.1,
    }

    def __init__(self, model, epochs=100, lr=0.01, explain_graph=False, molecule=False):
        super(MyVulExplainer, self).__init__(model, epochs, lr, explain_graph, molecule)



    def __loss__(self, raw_preds, x_label):
        if self.explain_graph:
            loss = Metric.loss_func(raw_preds, x_label)
        else:
            loss = Metric.loss_func(raw_preds[self.node_idx].unsqueeze(0), x_label)

        m = self.edge_mask.sigmoid()
        loss = loss + self.coeffs['edge_size'] * m.sum()
        ent = -m * torch.log(m + EPS) - (1 - m) * torch.log(1 - m + EPS)
        loss = loss + self.coeffs['edge_ent'] * ent.mean()

        if self.mask_features:
            m = self.node_feat_mask.sigmoid()
            loss = loss + self.coeffs['node_feat_size'] * m.sum()
            ent = -m * torch.log(m + EPS) - (1 - m) * torch.log(1 - m + EPS)
            loss = loss + self.coeffs['node_feat_ent'] * ent.mean()

        return loss


    def gnn_explainer_alg(self,
                          x: Tensor,
                          edge_index: Tensor,
                          edge_attr:Tensor,
                          ex_label: Tensor,
                          mask_features: bool = False,
                          **kwargs
                          ) -> None:

        # initialize a mask
        self.to(x.device)
        self.mask_features = mask_features

        # train to get the mask
        optimizer = torch.optim.Adam([self.node_feat_mask, self.edge_mask],
                                     lr=self.lr)
        
        #loss_function = BCELoss(reduction='sum')

        for epoch in range(1, self.epochs + 1):

            if mask_features:
                h = x * self.node_feat_mask.view(1, -1).sigmoid()
            else:
                h = x

            #graph = self.type_conversion(h, edge_index, edge_attr)

            #raw_preds = self.model(graph, cuda=True, **kwargs)
            raw_preds = self.model(h, edge_index)
            loss = self.__loss__(raw_preds, ex_label)
            #loss = loss_function(raw_preds, ex_label)
            if epoch % 20 == 0:
                print(f'#D#Loss:{loss.item()}')

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        return self.edge_mask.data

    def forward(self, data, mask_features=False,
                positive=True, **kwargs):
        r"""Learns and returns a node feature mask and an edge mask that play a
        crucial role to explain the prediction made by the GNN for node
        :attr:`node_idx`.

        Args:
            data (Batch): batch from dataloader
            edge_index (LongTensor): The edge indices.
            pos_neg (Literal['pos', 'neg']) : get positive or negative mask
            **kwargs (optional): Additional arguments passed to the GNN module.

        :rtype: (:class:`Tensor`, :class:`Tensor`)
        """
        x = data.x
        edge_index = data.edge_index
        edge_attr = data.edge_attr
        name = data.name[0]
        self.model.eval()
        node_index = {}
        slice_x = x
        slice_edge_index = edge_index
        sparsity=kwargs.get('sparsity')
        sparsity_num = int(x.shape[0]*(1-sparsity))+1
        res_coalition = []
        try:
            func_preds = self.model(x, edge_index)
        except Exception as E_results:
            print('model error: ',E_results)
            return
        func_score = func_preds[:, 1]
        _, predicted = func_preds.max(1)
        if int(predicted) == 0:
            print("predict = 0")
            return
        dot_name = '/home/VulGnnExp/com_pdg/1_vul/'+name.split('.json')[0]+'.dot' ###dot_path
        nx_pdg = nx.drawing.nx_pydot.read_dot(dot_name)
        if type(nx_pdg) != None:
            for index, node in enumerate(nx_pdg.nodes()):
                if node.startswith('1'):
                    node_index[index] = node
                    node_index[node] = index
        else:
            return
        if data_args.model_level == 'node':
            node_idx = kwargs.get('node_idx')
            self.node_idx = node_idx
            assert node_idx is not None
            _, _, _, self.hard_edge_mask = subgraph(
                node_idx, self.__num_hops__, edge_index, relabel_nodes=True,
                num_nodes=None, flow=self.__flow__())

        labels = tuple(i for i in range(data_args.num_classes))

        ex_labels = tuple(torch.Tensor([label]).to(data_args.device) for label in labels)

        # Calculate mask
        print('#D#Masks calculate...')
        for i, ex_label in enumerate(ex_labels):
            if i == 0:
                continue
            self.__clear_masks__()
            # self.__set_masks__(x, self_loop_edge_index)
            self.__set_masks__(x, edge_index)
            sorted_node_in, sorted_node_out, node_dict = self.control_sparsity(slice_edge_index, self.gnn_explainer_alg(slice_x, slice_edge_index, edge_attr, ex_label), name)
            if len(sorted_node_in) > len(sorted_node_out):
                sorted_node_list = sorted_node_in
            else:
                sorted_node_list = sorted_node_out
            func_res_coalition = []
            func_res_coalition2 = []

            for sorted_node in sorted_node_list[:sparsity_num]:
            # for selected_node in sorted_node[:sparsity_num]:
                pre_coalition = []
                suc_coalition =[]
                # if sorted_node not in node_index:
                #     continue
                sel_node = node_index[sorted_node]
                pre_coalition.append(sel_node)
                suc_coalition.append(sel_node)
                pre_coalition = self.rollin(nx_pdg,sel_node,pre_coalition,node_index,sparsity_num,node_dict)
                suc_coalition = self.rollout(nx_pdg,sel_node,suc_coalition,node_index,sparsity_num,node_dict)
                func_res_item = list(set(pre_coalition+suc_coalition))
                if len(func_res_item) >= int(sparsity_num/2) and len(func_res_item) <= int(sparsity_num*2):
                    sorted_slice_item = sorted(func_res_item)
                    if sorted_slice_item not in func_res_coalition:
                        func_res_coalition.append(sorted_slice_item)
                if len(func_res_item) <= sparsity_num*2:
                    sorted_slice_item2 = sorted(func_res_item)
                    if sorted_slice_item2 not in func_res_coalition2:
                        func_res_coalition2.append(sorted_slice_item2)
            exp_dict={}
            if func_res_coalition==[]:
                func_res_coalition=func_res_coalition2
            if func_res_coalition!=[]:
                for coalition_item in func_res_coalition:
                    node_mask = np.ones(x.shape[0])
                    coalition_item_sorted = []
                    for node_item in  coalition_item:
                        coalition_item_sorted.append(node_index[node_item])
                    node_mask[coalition_item_sorted] = 0.0
                    node_mask = torch.tensor(node_mask).type(torch.float32).to(x.device)
                    exclude_xi,exclude_edgeindexi=self.graph_build_zero_split(x,edge_index,node_mask)
                    exclude_preds = self.model(exclude_xi,exclude_edgeindexi)
                    exclude_score = exclude_preds[:, 1]
                    exp_dict[tuple(coalition_item)] = func_score-exclude_score
                exp_dict = sorted(exp_dict.items(),key=lambda x:x[1],reverse=True)
                exp_sel_func = exp_dict[0]
                res_coalition.append(exp_sel_func[0])
            # else:
            #     print('no res')
            for index,exp_item in enumerate(res_coalition):
                exp_name = name.split('.json')[0]+'###expfunc'+'.json'
                exp_path = '/home/VulGnnExp/VGExplainer/xxxx/'+ name.split('.json')[0]+'/' ###glob-view output
                if os.path.exists(exp_path):
                    exp_write_path = exp_path + exp_name
                    with open(exp_write_path,'w') as wf:
                        json.dump(list(exp_item),wf)
                else:
                    os.mkdir(exp_path)
                    exp_write_path = exp_path + exp_name
                    with open(exp_write_path,'w') as wf:
                        json.dump(list(exp_item),wf)



        res_coalition = []
        slice_path = '/home/VulGnnExp/slice_pdg/'+name.split('.json')[0] ###slice_path
        slice_lists = glob.glob(slice_path+'/*')
        if slice_lists != []:
            slice_dict = {}
            slice_score_dict ={}
            print('Read Slices...')
            for slice_item in tqdm(slice_lists):
                slice_nodes=[]
                try:
                    slice_pdg = nx.drawing.nx_pydot.read_dot(slice_item)
                except:
                    continue
                for node in slice_pdg.nodes():
                    if node.startswith('1'):
                        slice_nodes.append(node_index[node])
                slice_edges = []
                for item in slice_pdg.adj.items():
                    s = item[0]
                    for edge_relation in item[1]:
                        d = edge_relation    
                        slice_edges.append((node_index[s], node_index[d]))
                node_mask = np.zeros(x.shape[0])
                node_mask[slice_nodes] = 1.0
                node_mask = torch.tensor(node_mask).type(torch.float32).to(x.device)
                slice_xi,slice_edgeindexi = self.get_slicegraph(x, slice_edges, node_mask)
                slice_preds = self.model(slice_xi, edge_index)
                _, slice_predict = slice_preds.max(1)
                slice_preds = self.model(slice_xi, slice_edgeindexi)
                slice_score = slice_preds[:, 1]
                if(slice_predict == 1 and len(slice_nodes)>sparsity_num):
                    slice_dict[tuple([slice_xi,slice_edgeindexi,slice_pdg])] = slice_score
                if(slice_predict == 1 and len(slice_nodes)<=sparsity_num and len(slice_nodes)>=int(sparsity_num/2)):
                    for index,slice_node in enumerate(slice_nodes):
                        slice_nodes[index] = node_index[slice_node]
                    res_coalition.append(slice_nodes)
            if slice_dict != {}:
                slice_dict = sorted(slice_dict.items(),key=lambda x:x[1],reverse=False)
                for slice_item in slice_dict:
                    slice_res_coalition = []
                    slice_res_coalition2 = []
                    keys_list = slice_item
                    first_key = keys_list[0]
                    slice_x,slice_edge_index,slice_pdg =  first_key[0],first_key[1],first_key[2]
                    slice_score = float(keys_list[1])

                    # self_loop_edge_index, _ = add_self_loops(edge_index, num_nodes=x.shape[0])
                    #self_loop_edge_index, _ = add_self_loops(slice_edge_index, num_nodes=slice_x.shape[0])

                    # Only operate on a k-hop subgraph around `node_idx`.
                    # Get subgraph and relabel the node, mapping is the relabeled given node_idx.
                    if data_args.model_level == 'node':
                        node_idx = kwargs.get('node_idx')
                        self.node_idx = node_idx
                        assert node_idx is not None
                        _, _, _, self.hard_edge_mask = subgraph(
                            node_idx, self.__num_hops__, edge_index, relabel_nodes=True,
                            num_nodes=None, flow=self.__flow__())

                    # Assume the mask we will predict, dataset.py -> load_dataset() -> bcs -> 2
                    labels = tuple(i for i in range(data_args.num_classes))
                    # ex_label : label -> transform type to tensor[]  
                    ex_labels = tuple(torch.Tensor([label]).to(data_args.device) for label in labels)

                    # Calculate mask
                    print('#D#Masks calculate...')
                    for i, ex_label in enumerate(ex_labels):
                        if i == 0:
                            continue
                        self.__clear_masks__()
                        # self.__set_masks__(x, self_loop_edge_index)
                        self.__set_masks__(slice_x, slice_edge_index)
                        sorted_node_in, sorted_node_out, node_dict = self.control_sparsity(slice_edge_index, self.gnn_explainer_alg(slice_x, slice_edge_index, edge_attr, ex_label), name)
                        if len(sorted_node_in) > len(sorted_node_out):
                            sorted_node_list = sorted_node_in
                        else:
                            sorted_node_list = sorted_node_out   
                     

                        for sorted_node in sorted_node_list[:sparsity_num]:
                        # for selected_node in sorted_node[:sparsity_num]:
                            pre_coalition = []
                            suc_coalition =[]
                            sel_node = node_index[sorted_node]
                            pre_coalition.append(sel_node)
                            suc_coalition.append(sel_node)
                            pre_coalition = self.rollin(slice_pdg,sel_node,pre_coalition,node_index,sparsity_num,node_dict)
                            suc_coalition = self.rollout(slice_pdg,sel_node,suc_coalition,node_index,sparsity_num,node_dict)
                            slice_res_item = list(set(pre_coalition+suc_coalition))
                            if len(slice_res_item) >= int(sparsity_num/2) and len(slice_res_item) <= int(sparsity_num*2):
                                slice_sorted_item = sorted(slice_res_item)
                                if slice_sorted_item not in slice_res_coalition:
                                    slice_res_coalition.append(slice_sorted_item)
                            if len(slice_res_item) <= sparsity_num*2:
                                slice_sorted_item2 = sorted(slice_res_item)
                                if slice_sorted_item2 not in slice_res_coalition2:
                                    slice_res_coalition2.append(slice_sorted_item2)
                        exp_dict={}
                        if slice_res_coalition ==[]:
                            slice_res_coalition=slice_res_coalition2
                        if slice_res_coalition!=[]:
                            for coalition_item in slice_res_coalition:
                                node_mask = np.ones(slice_x.shape[0])
                                coalition_item_sorted = []
                                for node_item in  coalition_item:
                                    coalition_item_sorted.append(node_index[node_item])
                                node_mask[coalition_item_sorted] = 0.0
                                node_mask = torch.tensor(node_mask).type(torch.float32).to(slice_x.device)
                                exclude_xi,exclude_edgeindexi=self.graph_build_zero_split(slice_x,slice_edge_index,node_mask)
                                exclude_preds = self.model(exclude_xi,exclude_edgeindexi)
                                exclude_score = exclude_preds[:, 1]
                                exp_dict[tuple(coalition_item)] = slice_score-exclude_score
                            exp_dict = sorted(exp_dict.items(),key=lambda x:x[1],reverse=True)
                            exp_sel_slice = exp_dict[0]
                            res_coalition.append(exp_sel_slice[0])

                for index,exp_item in enumerate(res_coalition): 
                    node_mask = np.ones(x.shape[0])
                    coalition_res_sorted = []
                    for node_item in  exp_item:
                        coalition_res_sorted.append(node_index[node_item])
                    node_mask[coalition_res_sorted] = 0.0
                    node_mask = torch.tensor(node_mask).type(torch.float32).to(x.device)
                    exclude_xi,exclude_edgeindexi=self.graph_build_zero_split(x,edge_index,node_mask)
                    exclude_preds = self.model(exclude_xi,exclude_edgeindexi)
                    exclude_score = exclude_preds[:, 1]
                    slice_score_dict[tuple(exp_item)] = func_score-exclude_score
                slice_score_dict=sorted(slice_score_dict.items(),key=lambda x:x[1],reverse=True)
                    
                exp_name = name.split('.json')[0]+'###expslice'+'.json'
                exp_path = '/home/VulGnnExp/VGExplainer/xxxxx/'+ name.split('.json')[0]+'/'  ###local_view output
                for index,slice_pair in enumerate(slice_score_dict):
                    if os.path.exists(exp_path):
                        exp_write_path = exp_path + exp_name.split('.json')[0]+str(index)+'.json'
                        with open(exp_write_path,'w') as wf:
                            # json.dump(list(slice_score_dict[0][0]),wf)
                            json.dump(list(slice_pair[0]),wf)
                    else:
                        os.mkdir(exp_path)
                        exp_write_path = exp_path + exp_name
                        with open(exp_write_path,'w') as wf:
                            json.dump(list(slice_pair[0]),wf)
                exp_only_name = name.split('.json')[0]+'###exponly'+'.json'    ###best explanation
                if exp_sel_func[1] > slice_score_dict[0][1]:
                    if os.path.exists(exp_path):
                        exp_write_path = exp_path + exp_only_name
                        with open(exp_write_path,'w') as wf:
                            json.dump(list(exp_sel_func[0]),wf)
                    else:
                        os.mkdir(exp_path)
                        exp_write_path = exp_path + exp_only_name
                        with open(exp_write_path,'w') as wf:
                            json.dump(list(exp_sel_func[0]),wf)
                else:
                    if os.path.exists(exp_path):
                        exp_write_path = exp_path + exp_only_name
                        with open(exp_write_path,'w') as wf:
                            json.dump(list(slice_score_dict[0][0]),wf)
                    else:
                        os.mkdir(exp_path)
                        exp_write_path = exp_path + exp_only_name
                        with open(exp_write_path,'w') as wf:
                            json.dump(list(slice_score_dict[0][0]),wf)


            
        print('#D#Predict over ...')
        
        return



    def __repr__(self):
        return f'{self.__class__.__name__}()'

    def rollin(self, nx_pdg,sel_node,coalition,node_index,k_threshold,node_dict,):
        predecessors = list(nx_pdg.predecessors(sel_node))
        if predecessors==[] or len(coalition)>k_threshold:
            return coalition
        for index,predecessor in enumerate(predecessors):
            predecessors[index] = node_index[predecessor]
        for predecessor in predecessors:
            if node_index[predecessor] not in coalition:
                if node_dict[predecessor] < k_threshold:
                    coalition.append(node_index[predecessor])
                    coalition = self.rollin(nx_pdg,node_index[predecessor],coalition,node_index,k_threshold,node_dict)
        return coalition

    def rollout(self, nx_pdg,sel_node,coalition,node_index,k_threshold,node_dict):
        neighbors = list(nx_pdg.neighbors(sel_node))
        if neighbors==[] or len(coalition)>k_threshold:
            return coalition
        for index,neighbor in enumerate(neighbors):
            neighbors[index] = node_index[neighbor]
        for neighbor in neighbors:
            if node_index[neighbor] not in coalition:
                if node_dict[neighbor] < k_threshold:
                    coalition.append(node_index[neighbor])
                    coalition = self.rollout(nx_pdg,node_index[neighbor],coalition,node_index,k_threshold,node_dict)
        return coalition
    

    def get_slicegraph(self, X, edge_list, node_mask: np.array):
        ret_X = X * node_mask.unsqueeze(1)
        num_nodes = X.shape[0]
        edge_index_list = []
        for edge in edge_list:
            if edge[0] <= num_nodes and edge[1] <= num_nodes:
                edge_index_list.append([edge[0],edge[1]])
        edge_index = torch.tensor(edge_index_list,dtype=torch.long).t().to(X.device)
        return ret_X, edge_index
        
    def graph_build_zero_split(self, X, edge_index, node_mask: np.array):
        ret_X = X * node_mask.unsqueeze(1)
        row, col = edge_index
        edge_mask = (node_mask[row] == 1) & (node_mask[col] == 1)
        ret_edge_index = edge_index[:, edge_mask]
        return ret_X, ret_edge_index
    
    def graph_build_zero_padding(self, X, edge_index, node_mask: np.array):
        ret_X = X * node_mask.unsqueeze(1)
        # row, col = edge_index
        # edge_mask = (node_mask[row] == 1) & (node_mask[col] == 1)
        # ret_edge_index = edge_index[:, edge_mask]
        return ret_X, edge_index
