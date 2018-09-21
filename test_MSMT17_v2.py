from __future__ import print_function

import torch
import torch.nn as nn
import numpy as np
from torch.autograd import Variable
from person_reid_nets import Siamese_Net
import resnet_custom
import torchvision.models as models
# from torchsummary import summary    # sigh not working on pyhon 2.7 Sep 2018


def two_margin_cosine_embedding_loss(input1, input2, target, pos_margin=0.0, neg_margin=0.0):
    # Computes cosine loss with soft margins for positive and negative samples.
    # 
    # Example of soft margin:
    #     pos_margin = 0.1
    #     Loss contributed by a positive pair is 0 if cosine distance is greater than 0.9 (0.9 == 1 - 0.1).
    #
    # Motivation:
    #     cosine distance has range [-1, +1].
    #     similar vectors should be closer to +1.    (note: target 1)
    #     orthogonal vectors should give 0.
    #     dissimilar vectors should be closer to -1. (note: target 0)
    #     But, many a times, dissimilar/similar vectors aren't perfectly alike.
    #     For example, the same person's attire appearing slightly differently
    #     under different pose and/or illumination conditions - in such a 
    #     case, vectors representing him, v1 & v2, should be close to +1 but
    #     it may not make sense to "force" dist(v1, v2) to be exactly +1.
    #     Dissimilar vectors, v3 & v4, by the same logic, also should not be
    #     forced to be exactly -1. There could a be another dissimilar vector, v5,
    #     that is closer to, say, v3 conceptually. It is better to have dist(v5,v3) < dist(v5,v4)
    #     than "force" dist(v5,v3) == dist(v5,v4) == -1.
    #
    # Arguments:
    #     input1 & input2 are shaped (n_samples, dimensions)
    #     target is shaped (n_samples,). Either 1 or 0. 1 is positive/similar pair. 0 is negative/dissimilar pair.
    D = torch.nn.functional.cosine_similarity(input1, input2)
    device = D.device
    zeroes = torch.zeros(target.shape[0]).to(device)
    D_pos = 1.0 - pos_margin - D
    D_neg = D - neg_margin
    Z_D_pos = torch.stack([zeroes, D_pos], dim=1)
    Z_D_neg = torch.stack([zeroes, D_neg], dim=1)
    loss = (1.0*target) * torch.max(Z_D_pos, dim=1)[0]  +  (1.0 - target) * torch.max(Z_D_neg, dim=1)[0]
    loss = torch.sum(loss) / (1.0 * target.shape[0])
    return loss

def test_two_margin_cosine_embedding_loss():
    for repeat in range(100):
        i1 = torch.randn(10, 2)  * torch.randint(2, 100, (10,2))
        i2 = torch.randn(10, 2) * torch.randint(2, 100, (10,2))
        y = torch.randint(0, 2, (10,))
        l2 = two_margin_cosine_embedding_loss(i1, i2, y)
        loss1 = torch.nn.CosineEmbeddingLoss()
        y[y < 0.5] = -1
        l1 = loss1(i1, i2, y)
        d = l2 - l1
        assert(np.abs(d.numpy()) < 1e-6)

class ContrastiveLoss(torch.nn.Module):
    """
    Contrastive loss function.
    Based on:
    """
    def __init__(self, option='cosine', eucl_margin=1.0, margin=0.0, pos_margin=0.0, neg_margin=0.0):
        super(ContrastiveLoss, self).__init__()
        if option == 'euclidean':
            self.eucl_margin = eucl_margin
        elif option == 'cosine':
            self.loss = torch.nn.CosineEmbeddingLoss(margin=margin) # https://pytorch.org/docs/stable/nn.html#torch.nn.CosineEmbeddingLoss
        elif option == 'two margin cosine':
            test_two_margin_cosine_embedding_loss() # quick test
            self.pos_margin = pos_margin
            self.neg_margin = neg_margin
        else:
            raise Exception('No such option.')
        self.option = option

    def check_type_forward(self, in_types):
        assert len(in_types) == 3
        x0_type, x1_type, y_type = in_types
        assert x0_type.size() == x1_type.shape
        print(x1_type.size() , y_type.shape)
        assert x1_type.size()[0] == y_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y_type.dim() == 1

    def forward(self, x0, x1, y):
        self.check_type_forward((x0, x1, y))
        if self.option == 'euclidean':
            diff = x0 - x1
            dist_sq = torch.sum(torch.pow(diff, 2), 1)
            dist = torch.sqrt(dist_sq)
            mdist = self.eucl_margin - dist
            dist = torch.clamp(mdist, min=0.0)
            loss = (1.0*y) * dist_sq + (1.0 - y) * torch.pow(dist, 2)
            loss = torch.sum(loss) / (2.0 * x0.size()[0])        # is really sum(loss) / 2 / n_samps
            return loss
        elif self.option == 'cosine':
            y[y < 0.2] = -1 # replace 0s with -1
            return self.loss(x0, x1, y)
        elif self.option == 'two margin cosine':
            return two_margin_cosine_embedding_loss(x0, x1, y, pos_margin=self.pos_margin, neg_margin=self.neg_margin)
        else:
            raise Exception('No such option.')
        

def scores(embeds1, embeds2, metric):
    # embeds1 and embeds2 are numpy arrays of shape (n_samples, n_features)
    # where embeds1[i,:] and embeds2[i,:] represent embeddings from
    # the SAME person.
    if metric == 'cosine':
        D = pairwise_distances(embeds1, embeds2, metric='cosine')
        D = (D - 1.0) * -1.0  # Compute similarity from distances.
        non_diag_coords = np.where(~np.eye(D.shape[0],dtype=bool))
        imposter_scores = D[non_diag_coords]
        genuine_scores = np.diagonal(D)
        return D, genuine_scores, imposter_scores  # Returns a distance matrix ("scipy convention"), genuine scores, imposter scores.
    else:
        raise NotImplementedError


def plot_scores(genuines, imposters, title, bin_width):
    fig = plt.figure()
    ax = fig.gca()
    plt.title(title)
    max_score = max(max(genuines), max(imposters))
    min_score = min(min(genuines), min(imposters))
    bins = np.arange(min_score, max_score + bin_width, bin_width)
    ax.hist(genuines, bins=bins, color='g', alpha = 0.8)
    ax.hist(imposters, bins=bins, color='r', alpha = 0.4)
    return fig, plt

import math
import random
import torch.optim as optim
import torchvision.models as models
import os
import argparse
import pickle
from datetime import datetime
from sklearn.metrics.pairwise import pairwise_distances
import matplotlib.pyplot as plt
from person_reid_nets import Siamese_Net
import resnet_custom
import msmt17_v1_utils
from IUV_stack_utils import *  #TODO
from cmc import count as cmc_count


def combined_IUVstack_from_multiple_chips(dataload, pid, chip_paths, trainortest, combine_mode=None):
    # example:
    # combined_IUV_stack = chip_paths_to_combined_IUVstack(pid='0001', 
    #                                                      chip_paths=['/data/MSMT17_V1/train/0001/0001_011_01_0303morning_0036_1.jpg'
    #                                                   '/data/MSMT17_V1/train/0001/0001_051_01_0303morning_0758_0.jpg'
    #                                                   '/data/MSMT17_V1/train/0001/0001_045_01_0303morning_0755_1.jpg'
    #                                                   '/data/MSMT17_V1/train/0001/0001_067_14_0303morning_0981_1.jpg'
    #                                                   '/data/MSMT17_V1/train/0001/0001_018_07_0303morning_0033_1_ex.jpg'], 
    #                                                    trainortest = 'train')
    indiv_stacks = []
    for path in chip_paths:
        chipname = os.path.basename(path)
        im, IUV, INDS = dataload.get(trainortest=trainortest, 
                                     pid=pid, 
                                     chipname=chipname)
        if IUV is None or INDS is None:
            continue # skip processing this path
        ii = inds_value_of_most_prominent_person(INDS, mode='strict')
        if ii is None:   # strict mode "failed". Change mode.
            ii = inds_value_of_most_prominent_person(INDS, mode='most pixels')
        if ii is None:   # could be that INDS segmented no person(s).
            continue # skip processing this path
        new_IUV = IUV_with_only_this_inds_value(IUV, INDS, inds_value=ii)
        indiv_stacks.append(create_IUVstack(im, new_IUV))
    return combine_IUV_stacks(IUV_stack_list=indiv_stacks, mode=combine_mode)


def preprocess_IUV_stack(IUV_stack, device):
    # IUV_stack: Format is format of output of create_IUVstack(image_file, IUV_png_file).
    #            This code can be MODIFIED to also take in a HSV or HS (no V) type of IUVstack.
    # device: torch device. E.g. 'cuda' or 'cpu'.
    S = IUV_stack[:, 16:256-16, 16:256-16, :]    # Crop to 24x224x224x3
    S = normalize_to_reals_0to1(S)               # Normalize
    S = torch.Tensor(S)                          # Convert to torch tensors
    S = S.to(device)
    return S.view(24*3, 224, 224)


def make_pairs(batch_of_IUV_stacks, device):
    # batch_of_IUV_stacks is like [(S11,S12), (S21,S22), (S31,S32), ... (Sn1,Sn2)]
    #                              S11 is stack 1 of person id 1. S12 is stack 2 of person 2.
    #                               Sxx is an IUV stack.
    input1s = []; input2s = []; targets = []
    for person in range(len(batch_of_IUV_stacks)):
        ######## Same person pair ########
        S1 = batch_of_IUV_stacks[person][0]
        S2 = batch_of_IUV_stacks[person][1]
        mask = get_intersection(S1, S2)          # intersect
        # most_info_in_an_input_so_far = max(np.sum(mask), most_info_in_an_input_so_far) # update
        #print('% IUV filled: ', 1.0 * np.sum(mask) / most_info_in_an_input_so_far)
        S1 = apply_mask_to_IUVstack(S1, mask)
        S2 = apply_mask_to_IUVstack(S2, mask)
        S1 = preprocess_IUV_stack(S1, device)
        S2 = preprocess_IUV_stack(S2, device)
        input1s.append(S1); input2s.append(S2); targets.append(1)
        ######## Diff persons pair ########
        persons = list(range(len(batch_of_IUV_stacks)))
        persons.remove(person)
        another_person = random.choice(persons)
        assert(person != another_person) # prevent edge cases like 1 person only in batch.
        S1 = batch_of_IUV_stacks[person][random.randint(0,1)]
        S2 = batch_of_IUV_stacks[another_person][random.randint(0,1)]
        mask = get_intersection(S1, S2)          # intersect
        # most_info_in_an_input_so_far = max(np.sum(mask), most_info_in_an_input_so_far) # update
        # print('% IUV filled: ', 1.0 * np.sum(mask) / most_info_in_an_input_so_far)
        S1 = apply_mask_to_IUVstack(S1, mask)
        S2 = apply_mask_to_IUVstack(S2, mask)
        S1 = preprocess_IUV_stack(S1, device)
        S2 = preprocess_IUV_stack(S2, device)
        input1s.append(S1); input2s.append(S2); targets.append(0)
    assert(len(input1s) > 0)
    assert( len(input1s) == len(input2s) and len(input2s) == len(targets) )
    return input1s, input2s, targets


class Dataset_Test(torch.utils.data.Dataset):
    def __init__(self, precomputed_test_path, setid, glabels, plabels):
        """
        Args:
            precomputed_test_path (string): e.g. '/data/IUV-densepose/MSMT17_V1/precomputed'
            glabels: pids of gallery people e.g. [12, 44, 55, 37, 897, 1034, ... ]
            plabels: pids of probe people e.g. [12, 44, 55, 37]
        """
        super(Dataset_Test, self).__init__()
        assert(all([pid == glabels[i] for i, pid in enumerate(plabels)]))
        self.glabels = list(glabels) # deep-copy or convert to list
        self.plabels = list(plabels) # deep-copy or convert to list
        glabels, plabels = None, None  # safety
        self.gpicks = []
        self.ppicks = []
        for i, gpid in enumerate(self.glabels):
            if i < len(self.plabels): 
                ppid = self.plabels[i]
                print('g an dp pid', gpid, ppid)
                assert(gpid == ppid)
                picks = np.random.permutation([1, 2])
                self.gpicks.append(picks[0])
                self.ppicks.append(picks[1])
            else:
                assert(i >= len(self.plabels))
                self.gpicks.append(random.randint(1,2))
        assert(len(self.gpicks) == len(self.glabels))
        assert(len(self.ppicks) == len(self.plabels))
        self.shape_of_similarity_matrix = (len(self.glabels), len(self.plabels)) # This is the convention by cysu's cmc.
        # shape_of_similarity_matrix convention:
        # going down rows is going thru gallery ppl.
        # going along columns is going thru probe ppl.
        # ----
        self.precomputed_test_path = precomputed_test_path
        self.setid = setid

    def __len__(self):
        return self.shape_of_similarity_matrix[0] * self.shape_of_similarity_matrix[1]

    def read_IUV_stack(self, setid, pid, pick):
        # e.g. setid = 1, pid = 432, pick = 1 or 0
        IUVs = np.load(os.path.join(self.precomputed_test_path, str(setid), str(pid)+'.npz'  ))
        S = IUVs['S'+str(pick)].copy()
        return S

    def __getitem__(self, index):
        row_idx, col_idx = np.unravel_index(indices=index, 
                                            dims=self.shape_of_similarity_matrix, order='F') # F means assume column-major.
        # Convention:
        # go down rows is go thru gallery ppl.
        # go along columns is go thru probe ppl.
        # advancing index is go down rows. If at last row, go to first row of next column.
        g_pid = self.glabels[row_idx]
        g_pick = self.gpicks[row_idx]
        p_pid = self.plabels[col_idx]
        p_pick = self.ppicks[col_idx]
        S_gal = self.read_IUV_stack(self.setid, g_pid, g_pick)
        S_probe = self.read_IUV_stack(self.setid, p_pid, p_pick)
        mask = get_intersection(S_gal, S_probe)
        intersection_amt = np.sum(mask)
        # most_info_in_an_input_so_far = max(np.sum(mask), most_info_in_an_input_so_far) # update
        # print('% IUV filled: ', 1.0 * np.sum(mask) / most_info_in_an_input_so_far)
        Sp = apply_mask_to_IUVstack(S_probe.copy(), mask)
        Sg = apply_mask_to_IUVstack(S_gal.copy(), mask)
        Sp = preprocess_IUV_stack(Sp, device='cpu')
        Sg = preprocess_IUV_stack(Sg, device='cpu')
        target = 1 if g_pid==p_pid else 0
        target = torch.Tensor([target])
        # return [100], [200], target, 400, 500, 600, 700,  row_idx, col_idx, intersection_amt, index
        return Sg, Sp, target, g_pid, p_pid, g_pick, p_pick, row_idx, col_idx, intersection_amt, index
        #Sg is IUV stack of gallery. Sp is IUV stack of probe.



if __name__ == '__main__':
    script_start_time = "{:%m-%d-%H-%M-%S}".format(datetime.now()); print('script_start_time ', script_start_time)
    parser = argparse.ArgumentParser()
    parser.add_argument('--odir', type=str, default='tmp',
                        help='output dir. e.g. \'expt-1\'.')
    parser.add_argument('--idir', type=str, default='/data/IUV-densepose/MSMT17_V1/precomputed',
                        help='input dir.')
    parser.add_argument('--setid', type=int, default=0,
                        help='precomputed test set id')
    parser.add_argument('--device', type=str, default='cuda:3',
                        help='gpu or cpu device. cuda cuda:0 cuda:1 cpu')
    parser.add_argument('--n_probes', type=int, default=3,
                        help='num of probes')
    parser.add_argument('--n_gallery', type=int, default=3,
                        help='num of people in gallery')
    parser.add_argument('--batch_size', type=int, default=3,
                        help='To prevent OOM error on device running neural netowrk.')
    parser.add_argument('--num_workers', type=int, default=1,
                        help='num workers on dataloader.')
    parser.add_argument('--net', type=str, default=None,
                        help='path to network. e.g. \"expt-32-twomarg-0.1-0.7/net-ep-20.chkpt\"')
    parser.add_argument('--contrastlossopt', type=str, default="two margin cosine",
                        help='e.g. \"two margin cosine\"')
    parser.add_argument('--pos_margin', type=float, default=None,
                        help='e.g. 0.1')
    parser.add_argument('--neg_margin', type=float, default=None,
                        help='e.g. 0.7')
    parser.add_argument('--distance_type', type=str, default='cosine',
                        help='e.g. cosine')
    args = parser.parse_args()
    [print(arg, ':', getattr(args, arg)) for arg in vars(args)]
    assert(args.n_gallery >= args.n_probes)
    # assert(args.n_gallery % args.batch_size == 0)
    loss = ContrastiveLoss(option=args.contrastlossopt, pos_margin=args.pos_margin, neg_margin=args.neg_margin)
    print("[NOTE]  Ensure loss used is similar to loss used during training if you\'re comparing training loss to test/val loss.")
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    print('device: ', device)
    dataload = msmt17_v1_utils.MSMT17_V1_Load_Data_Utils(images_train_dir='/data/MSMT17_V1/train', 
                                                            images_test_dir='/data/MSMT17_V1/test', 
                                                            denseposeoutput_train_dir='/data/IUV-densepose/MSMT17_V1/train', 
                                                            denseposeoutput_test_dir='/data/IUV-densepose/MSMT17_V1/test')
    # Make dir for this experiment:
    if not os.path.exists(args.odir):
        os.makedirs(args.odir)
    logbk = {} # A dict for recording stuff computed.
    # Print loss:
    print(loss.__dict__)
    # Load net:
    net = resnet_custom.resnet18(input_channels=24*3, num_classes=256)
    net = Siamese_Net(net)
    net.load_state_dict(torch.load(args.net))
    net = net.to(device)
    net.eval()
    # Get samples:
    pids_shuffled = np.random.permutation(dataload.test_persons_cnt)
    glabels = pids_shuffled[:args.n_gallery]
    plabels = glabels[:args.n_probes]
    logbk['glabels'] = glabels
    logbk['plabels'] = plabels
    similarity_mat = np.zeros((len(glabels), len(plabels)))
    loss_mat = None # np.zeros_like(similarity_mat)  # TODO. loss needs to have reduce=False option.
    intersection_mat = np.zeros_like(similarity_mat)
    dataset_test = Dataset_Test(precomputed_test_path=args.idir, 
                                setid=args.setid, 
                                glabels=glabels, 
                                plabels=plabels)
    dataloader = torch.utils.data.DataLoader(dataset_test,
                                            batch_size=args.batch_size, 
                                            shuffle=False, # so can map to similarity matrix more easily (i guess?)
                                            num_workers=args.num_workers,
                                            drop_last = False)
    with torch.no_grad():
        for i_batch, data_batched in enumerate(dataloader):
            print(i_batch)
            S_gals, S_probes, targets, g_pids, p_pids, g_picks, p_picks, row_idxs, col_idxs, intersection_amts, flattened_idxs = data_batched
            print(S_gals.shape)
            print(targets.shape)
            print(flattened_idxs)
            print(row_idxs)
            print(col_idxs)

    exit()

    for x in wtf:






        for setid in range(1):
            for col, p_pid in enumerate(plabels):
                print('{} Probe [{}/ {}]:'.format("{:%m-%d-%H-%M-%S}".format(datetime.now()), 
                                                    col+1, 
                                                    len(plabels)))
                print('Doing col', col, 'whose probe pid is', p_pid)
                IUVs = np.load(os.path.join(args.idir, str(setid), str(p_pid)+'.npz'  ))
                S_probe = IUVs['S'+str(random.randint(1,2))].copy()
                assert(len(glabels) % args.batch_size == 0)
                num_batches_for_1_probe = len(glabels) / args.batch_size
                for bidx in range(num_batches_for_1_probe):
                    start_idx = bidx * args.batch_size
                    end_idx = start_idx + args.batch_size 
                    g_pids_batch = glabels[start_idx:end_idx] # end_idx not really the last index cuz half-open convention [a,b) in indexing.
                    print('{} Batch [{}/ {}]:'.format("{:%m-%d-%H-%M-%S}".format(datetime.now()), 
                                                        bidx+1, 
                                                        num_batches_for_1_probe))
                    print('glabels in this batch:', g_pids_batch)
                    input1s=[]; input2s=[]; targets=[]
                    for r, g_pid in enumerate(g_pids_batch):
                        IUVs = np.load(os.path.join(args.idir, str(setid), str(g_pid)+'.npz'  ))
                        S_gal = IUVs['S'+str(random.randint(1,2))].copy()
                        mask = get_intersection(S_probe, S_gal)
                        cur_row = start_idx + r
                        intersection_mat[cur_row, col] = np.sum(mask)
                        # most_info_in_an_input_so_far = max(np.sum(mask), most_info_in_an_input_so_far) # update
                        # print('% IUV filled: ', 1.0 * np.sum(mask) / most_info_in_an_input_so_far)
                        Sp = apply_mask_to_IUVstack(S_probe.copy(), mask)
                        Sg = apply_mask_to_IUVstack(S_gal.copy(), mask)
                        Sp = preprocess_IUV_stack(Sp, device)
                        Sg = preprocess_IUV_stack(Sg, device)
                        input1s.append(Sp); input2s.append(Sg); targets.append(1 if p_pid == g_pid else 0)
                    input1s, input2s, targets = torch.stack(input1s), torch.stack(input2s), torch.Tensor(targets)
                    input1s, input2s, targets = input1s.to(device), input2s.to(device), targets.to(device)
                    embs_p, embs_g = net(input1s, input2s)
                    #loss_ = loss(emb1s, emb2s, targets).cpu().numpy().copy()  # TODO add reduce so that two_margin_cosine_loss can do this.
                    # loss_mat[start_idx:end_idx, col] = loss_
                    score_mat, _, _ = scores(embs_g, embs_p, args.distance_type)
                    relevant_scores = np.diagonal(score_mat)  # off diagonal elems are irrelevant!
                    # for i in range(embs_g.shape[0]):
                    #     score, _, _ = scores(embs_g[[i],:], embs_p[[i],:], args.distance_type)
                    #     print(score)
                    similarity_mat[start_idx:end_idx, col] = relevant_scores
                    # print('intersection', intersection_mat[row, col])
                    # print('loss', loss_mat[row, col])
                    # print('similarity', similarity_mat[row, col])
                    # print(loss_mat[0:16,0:16])
                    #print(similarity_mat)
    logbk['similarity_mat'] = similarity_mat
    logbk['intersection_mat'] = intersection_mat
    pickle.dump(logbk, open(os.path.join(args.odir, 'test-logbk-{}.pkl'.format(script_start_time)), 'wb'), protocol=2)
    # Plot CMC:
    distmat = 1 - logbk['similarity_mat']
    cmc_values = cmc_count(distmat=distmat, glabels=logbk['glabels'], plabels=logbk['plabels'], n_selected_labels=None, n_repeat=1)
    print(cmc_values)
    fig = plt.figure()
    ax = fig.gca()
    plt.title('CMC')
    plt.plot(range(1, len(cmc_values) + 1), cmc_values, 'b')
    plt.ylabel('Match rate'); plt.xlabel('Rank')
    #plt.show()
    fig.savefig(os.path.join(args.odir, 'test-cmc-{}.jpg'.format(script_start_time)))
    plt.close(fig)

    


exit()
# --------------------------------------------------------------------------------------------



if __name__ == '__main__':
    script_start_time = "{:%m-%d-%H-%M-%S}".format(datetime.now()); print('script_start_time ', script_start_time)
    parser = argparse.ArgumentParser()
    parser.add_argument('--odir', type=str, default='tmp',
                        help='output dir. e.g. \'expt-1\'.')
    parser.add_argument('--idir', type=str, default='/data/IUV-densepose/MSMT17_V1/precomputed',
                        help='input dir.')
    parser.add_argument('--device', type=str, default='cuda:3',
                        help='gpu or cpu device. cuda cuda:0 cuda:1 cpu')
    parser.add_argument('--n_probes', type=int, default=3,
                        help='num of probes')
    parser.add_argument('--n_gallery', type=int, default=3,
                        help='num of people in gallery')
    parser.add_argument('--batch_size', type=int, default=3,
                        help='To prevent OOM error on device running neural netowrk.')
    parser.add_argument('--net', type=str, default=None,
                        help='path to network. e.g. \"expt-32-twomarg-0.1-0.7/net-ep-20.chkpt\"')
    parser.add_argument('--contrastlossopt', type=str, default="two margin cosine",
                        help='e.g. \"two margin cosine\"')
    parser.add_argument('--pos_margin', type=float, default=None,
                        help='e.g. 0.1')
    parser.add_argument('--neg_margin', type=float, default=None,
                        help='e.g. 0.7')
    parser.add_argument('--distance_type', type=str, default='cosine',
                        help='e.g. cosine')
    args = parser.parse_args()
    [print(arg, ':', getattr(args, arg)) for arg in vars(args)]
    assert(args.n_gallery >= args.n_probes)
    assert(args.n_gallery % args.batch_size == 0)
    loss = ContrastiveLoss(option=args.contrastlossopt, pos_margin=args.pos_margin, neg_margin=args.neg_margin)
    print("[NOTE]  Ensure loss used is similar to loss used during training if you\'re comparing training loss to test/val loss.")
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    print('device: ', device)
    dataload = msmt17_v1_utils.MSMT17_V1_Load_Data_Utils(images_train_dir='/data/MSMT17_V1/train', 
                                                            images_test_dir='/data/MSMT17_V1/test', 
                                                            denseposeoutput_train_dir='/data/IUV-densepose/MSMT17_V1/train', 
                                                            denseposeoutput_test_dir='/data/IUV-densepose/MSMT17_V1/test')
    # Make dir for this experiment:
    if not os.path.exists(args.odir):
        os.makedirs(args.odir)
    logbk = {} # A dict for recording stuff computed.
    # Print loss:
    print(loss.__dict__)
    # Load net:
    net = resnet_custom.resnet18(input_channels=24*3, num_classes=256)
    net = Siamese_Net(net)
    net.load_state_dict(torch.load(args.net))
    net = net.to(device)
    net.eval()
    # Get samples:
    pids_shuffled = np.random.permutation(dataload.test_persons_cnt)
    glabels = pids_shuffled[:args.n_gallery]
    plabels = glabels[:args.n_probes]
    logbk['glabels'] = glabels
    logbk['plabels'] = plabels
    similarity_mat = np.zeros((len(glabels), len(plabels)))
    loss_mat = None # np.zeros_like(similarity_mat)  # TODO. loss needs to have reduce=False option.
    intersection_mat = np.zeros_like(similarity_mat)
    with torch.no_grad():
        for setid in range(1):
            for col, p_pid in enumerate(plabels):
                print('{} Probe [{}/ {}]:'.format("{:%m-%d-%H-%M-%S}".format(datetime.now()), 
                                                    col+1, 
                                                    len(plabels)))
                print('Doing col', col, 'whose probe pid is', p_pid)
                IUVs = np.load(os.path.join(args.idir, str(setid), str(p_pid)+'.npz'  ))
                S_probe = IUVs['S'+str(random.randint(1,2))].copy()
                assert(len(glabels) % args.batch_size == 0)
                num_batches_for_1_probe = len(glabels) / args.batch_size
                for bidx in range(num_batches_for_1_probe):
                    start_idx = bidx * args.batch_size
                    end_idx = start_idx + args.batch_size 
                    g_pids_batch = glabels[start_idx:end_idx] # end_idx not really the last index cuz half-open convention [a,b) in indexing.
                    print('{} Batch [{}/ {}]:'.format("{:%m-%d-%H-%M-%S}".format(datetime.now()), 
                                                        bidx+1, 
                                                        num_batches_for_1_probe))
                    print('glabels in this batch:', g_pids_batch)
                    input1s=[]; input2s=[]; targets=[]
                    for r, g_pid in enumerate(g_pids_batch):
                        IUVs = np.load(os.path.join(args.idir, str(setid), str(g_pid)+'.npz'  ))
                        S_gal = IUVs['S'+str(random.randint(1,2))].copy()
                        mask = get_intersection(S_probe, S_gal)
                        cur_row = start_idx + r
                        intersection_mat[cur_row, col] = np.sum(mask)
                        # most_info_in_an_input_so_far = max(np.sum(mask), most_info_in_an_input_so_far) # update
                        # print('% IUV filled: ', 1.0 * np.sum(mask) / most_info_in_an_input_so_far)
                        Sp = apply_mask_to_IUVstack(S_probe.copy(), mask)
                        Sg = apply_mask_to_IUVstack(S_gal.copy(), mask)
                        Sp = preprocess_IUV_stack(Sp, device)
                        Sg = preprocess_IUV_stack(Sg, device)
                        input1s.append(Sp); input2s.append(Sg); targets.append(1 if p_pid == g_pid else 0)
                    input1s, input2s, targets = torch.stack(input1s), torch.stack(input2s), torch.Tensor(targets)
                    input1s, input2s, targets = input1s.to(device), input2s.to(device), targets.to(device)
                    embs_p, embs_g = net(input1s, input2s)
                    #loss_ = loss(emb1s, emb2s, targets).cpu().numpy().copy()  # TODO add reduce so that two_margin_cosine_loss can do this.
                    # loss_mat[start_idx:end_idx, col] = loss_
                    score_mat, _, _ = scores(embs_g, embs_p, args.distance_type)
                    relevant_scores = np.diagonal(score_mat)  # off diagonal elems are irrelevant!
                    # for i in range(embs_g.shape[0]):
                    #     score, _, _ = scores(embs_g[[i],:], embs_p[[i],:], args.distance_type)
                    #     print(score)
                    similarity_mat[start_idx:end_idx, col] = relevant_scores
                    # print('intersection', intersection_mat[row, col])
                    # print('loss', loss_mat[row, col])
                    # print('similarity', similarity_mat[row, col])
                    # print(loss_mat[0:16,0:16])
                    #print(similarity_mat)
    logbk['similarity_mat'] = similarity_mat
    logbk['intersection_mat'] = intersection_mat
    pickle.dump(logbk, open(os.path.join(args.odir, 'test-logbk-{}.pkl'.format(script_start_time)), 'wb'), protocol=2)
    # Plot CMC:
    distmat = 1 - logbk['similarity_mat']
    cmc_values = cmc_count(distmat=distmat, glabels=logbk['glabels'], plabels=logbk['plabels'], n_selected_labels=None, n_repeat=1)
    print(cmc_values)
    fig = plt.figure()
    ax = fig.gca()
    plt.title('CMC')
    plt.plot(range(1, len(cmc_values) + 1), cmc_values, 'b')
    plt.ylabel('Match rate'); plt.xlabel('Rank')
    #plt.show()
    fig.savefig(os.path.join(args.odir, 'test-cmc-{}.jpg'.format(script_start_time)))
    plt.close(fig)

    
    exit()



"""pidstr = str(pid).zfill(4)
n_chips_avail = dataload.num_chips('test', pidstr)
chips = dataload.random_chips('test', pidstr, n_chips_avail)
split1 = chips[ : int(len(chips)/2)]
split2 = chips[int(len(chips)/2) : ]
S1 = combined_IUVstack_from_multiple_chips(dataload, pid=pidstr, chip_paths=split1, trainortest='test', combine_mode='average v2')
S2 = combined_IUVstack_from_multiple_chips(dataload, pid=pidstr, chip_paths=split2, trainortest='test', combine_mode='average v2')
batch_of_IUV_stacks.append( (S1.copy(), S2.copy()) )
input1s, input2s, targets = make_pairs(batch_of_IUV_stacks, device='cpu')   # smaller batches!
# Compute loss:
with torch.no_grad():
input1s, input2s, targets = torch.stack(input1s), torch.stack(input2s), torch.Tensor(targets)
input1s, input2s, targets = input1s.to(device), input2s.to(device), targets.to(device) # yes! must re-assign
output1s, output2s = net(input1s, input2s)
loss_ = loss(output1s, output2s, targets)
logbk['loss'] = loss_.cpu().numpy().copy()
# print(output2s.shape)
# print(output1s.shape)
# print(targets.shape)
# embeds1_np.append(output1s.cpu().numpy().copy())
# embeds2_np.append(output2s.cpu().numpy().copy())
# targets_np.append(targets.cpu().numpy().copy())
# exit()
print('loss ', logbk['loss'])
# Compute metric genuine & imposter scores:
embeds1 = output1s[targets > 0.5,:].cpu().numpy().copy()
embeds2 = output2s[targets > 0.5,:].cpu().numpy().copy()
scoremat, genuine_scores, imposter_scores = scores(embeds1, embeds2, args.distance_type)
print('G', genuine_scores)
print('IMP', imposter_scores)
fig, plt = plot_scores(genuine_scores, imposter_scores, 'Scores', bin_width=0.05)
#plt.show()
fig.savefig(os.path.join(args.odir, 'test-scores-{}.jpg'.format(script_start_time)))
plt.close(fig)
logbk['scoremat'] = scoremat
logbk['genuine_scores'] = genuine_scores
logbk['imposter_scores'] = imposter_scores
# Compute metric CMC:
distmat = 1 - scoremat
logbk['distmat'] = distmat
cmc_values = cmc_count(distmat=distmat, n_selected_labels=None, n_repeat=1)
print(cmc_values)
logbk['cmc_values'] = cmc_values
fig = plt.figure()
ax = fig.gca()
plt.title('CMC')
plt.plot(range(1, len(cmc_values) + 1), cmc_values, 'b')
plt.ylabel('Match rate'); plt.xlabel('Rank')
#plt.show()
fig.savefig(os.path.join(args.odir, 'test-cmc-{}.jpg'.format(script_start_time)))
plt.close(fig)

for k,v in logbk.items():
print(k, v)
pickle.dump(logbk, open(os.path.join(args.odir, 'test-logbk-{}.pkl'.format(script_start_time)), 'wb'), protocol=2)"""