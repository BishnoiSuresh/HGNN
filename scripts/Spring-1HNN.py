################################################
################## IMPORT ######################
################################################

import json
import sys
import os
from datetime import datetime
from functools import partial, wraps

import fire
import jax
import jax.numpy as jnp
import numpy as np
from jax import jit, random, value_and_grad, vmap
from jax.experimental import optimizers
from jax_md import space
import matplotlib.pyplot as plt
from shadow.plot import *
from sklearn.metrics import r2_score
#from torch import mode
import time
from psystems.nsprings import (chain, edge_order, get_connections,
                               get_fully_connected_senders_and_receivers,
                               get_fully_edge_order)

MAINPATH = ".."  # nopep8
sys.path.append(MAINPATH)  # nopep8

import jraph
import src
from jax.config import config
from src import lnn
from src.graph import *
#from src.lnn import acceleration, accelerationFull, accelerationTV
from src.md import *
from src.models import MSE, initialize_mlp
from src.nve import nve
from src.utils import *
from src.hamiltonian import *

# config.update("jax_enable_x64", True)
# config.update("jax_debug_nans", True)
# jax.config.update('jax_platform_name', 'gpu')


def namestr(obj, namespace):
    return [name for name in namespace if namespace[name] is obj]


def pprint(*args, namespace=globals()):
    for arg in args:
        print(f"{namestr(arg, namespace)[0]}: {arg}")


def main(N=5, epochs=10000, seed=42, rname=True, saveat=10, error_fn="L2error",
         dt=1.0e-3, ifdrag=0, stride=100, trainm=1, grid=False, mpass=1, lr=0.001, withdata=None, datapoints=None, batch_size=100, config=None):
    
    print("Configs: ")
    pprint(N, epochs, seed, rname,
           dt, stride, lr, ifdrag, batch_size,
           namespace=locals())

    randfilename = datetime.now().strftime("%m-%d-%Y_%H-%M-%S") + f"_{datapoints}"
    
    PSYS = f"a-{N}-Spring"
    TAG = f"1HNN"
    out_dir = f"../results"
 
    def _filename(name, tag=TAG):
        rstring = randfilename if (rname and (tag != "data-ham")) else (
            "0" if (tag == "data-ham") or (withdata == None) else f"0_{withdata}")
        filename_prefix = f"{out_dir}/{PSYS}-{tag}/{rstring}/"
        file = f"{filename_prefix}/{name}"
        os.makedirs(os.path.dirname(file), exist_ok=True)
        filename = f"{filename_prefix}/{name}".replace("//", "/")
        print("===", filename, "===")
        return filename
    
    def OUT(f):
        @wraps(f)
        def func(file, *args, tag=TAG, **kwargs):
            return f(_filename(file, tag=tag), *args, **kwargs)
        return func

    loadmodel = OUT(src.models.loadmodel)
    savemodel = OUT(src.models.savemodel)

    loadfile = OUT(src.io.loadfile)
    savefile = OUT(src.io.savefile)
    save_ovito = OUT(src.io.save_ovito)
    
    ################################################
    ################## CONFIG ######################
    ################################################
    np.random.seed(seed)
    key = random.PRNGKey(seed)

    try:
        dataset_states = loadfile(f"model_states_{ifdrag}.pkl", tag="data-ham")[0]
    except:
        raise Exception("Generate dataset first.")
    
    model_states = dataset_states[0]
    z_out, zdot_out = model_states
    
    # print(
    #     f"Total number of data points: {len(dataset_states)}x{model_states.position.shape[0]}")
    print(
        f"Total number of data points: {len(dataset_states)}x{z_out.shape[0]}")

    N2, dim = z_out.shape[-2:]
    N = N2//2
    
    # N, dim = model_states.position.shape[-2:]
    # masses = model_states.mass[0].flatten()
    # species = jnp.zeros(N, dtype=int)
    
    array = jnp.array([jnp.array(i) for i in dataset_states])

    Zs = array[:, 0, :, :, :]
    Zs_dot = array[:, 1, :, :, :]

    Zs = Zs.reshape(-1, N2, dim)
    Zs_dot = Zs_dot.reshape(-1, N2, dim)
    
    mask = np.random.choice(len(Zs), len(Zs), replace=False)
    allZs = Zs[mask]
    allZs_dot = Zs_dot[mask]
    
    Ntr = int(0.75*len(Zs))
    Nts = len(Zs) - Ntr
    
    Zs = allZs[:Ntr]
    Zs_dot = allZs_dot[:Ntr]
    
    Zst = allZs[Ntr:]
    Zst_dot = allZs_dot[Ntr:]
    
    
    ################################################
    ################## SYSTEM ######################
    ################################################

    
    # def phi(x):
    #     X = jnp.vstack([x[:1, :]*0, x])
    #     return jnp.square(X[:-1, :] - X[1:, :]).sum(axis=1) - 1.0


    # constraints = get_constraints(N, dim, phi)

    ################################################
    ################### ML Model ###################
    ################################################
    
    def MLP(in_dim, out_dim, key, hidden=256, nhidden=2):
        return initialize_mlp([in_dim]+[hidden]*nhidden+[out_dim], key)

    lnn_params_pe = MLP(N*dim, 1, key)
    lnn_params_ke = jnp.array(np.random.randn(N))
    
    def Hmodel(x, v, params):
        return ((params["lnn_ke"] * jnp.square(v).sum(axis=1)).sum() +
                forward_pass(params["lnn_pe"], x.flatten(), activation_fn=SquarePlus)[0])
    
    params = {"lnn_pe": lnn_params_pe, "lnn_ke": lnn_params_ke}

    def nndrag(v, params):
        return - jnp.abs(models.forward_pass(params, v.reshape(-1), activation_fn=models.SquarePlus)) * v

    if ifdrag:
        print("Drag: learnable")

        def drag(x, v, params):
            return vmap(nndrag, in_axes=(0, None))(v.reshape(-1), params["drag"]).reshape(-1, 1)
    else:
        print("No drag.")

        def drag(x, v, params):
            return 0.0

    params["drag"] = initialize_mlp([1, 5, 5, 1], key)
    
    zdot_model, lamda_force_model = get_zdot_lambda(
        N, dim, hamiltonian=Hmodel, drag=None, constraints=None)
    
    
    v_zdot_model = vmap(zdot_model, in_axes=(0, 0, None))

    ################################################
    ################## ML Training #################
    ################################################
    LOSS = getattr(src.models, error_fn)

    @jit
    def loss_fn(params, Rs, Vs, Zs_dot):
        pred = v_zdot_model(Rs, Vs, params)
        return LOSS(pred, Zs_dot)
    @jit
    def gloss(*args):
        return value_and_grad(loss_fn)(*args)
    
    @jit
    def update(i, opt_state, params, loss__, *data):
        """ Compute the gradient for a batch and update the parameters """
        value, grads_ = gloss(params, *data)
        opt_state = opt_update(i, grads_, opt_state)
        return opt_state, get_params(opt_state), value
    
    @ jit
    def step(i, ps, *args):
        return update(i, *ps, *args)

    opt_init, opt_update_, get_params = optimizers.adam(lr)

    @ jit
    def opt_update(i, grads_, opt_state):
        grads_ = jax.tree_map(jnp.nan_to_num, grads_)
        # grads_ = jax.tree_map(partial(jnp.clip, a_min=-1000.0, a_max=1000.0), grads_)
        return opt_update_(i, grads_, opt_state)
    
    def batching(*args, size=None):
        L = len(args[0])
        if size != None:
            nbatches1 = int((L - 0.5) // size) + 1
            nbatches2 = max(1, nbatches1 - 1)
            size1 = int(L/nbatches1)
            size2 = int(L/nbatches2)
            if size1*nbatches1 > size2*nbatches2:
                size = size1
                nbatches = nbatches1
            else:
                size = size2
                nbatches = nbatches2
        else:
            nbatches = 1
            size = L

        newargs = []
        for arg in args:
            newargs += [jnp.array([arg[i*size:(i+1)*size]
                                   for i in range(nbatches)])]
        return newargs

    # bRs, bVs, bFs = batching(Rs, Vs, Fs, size=batch_size)
    Rs, Vs = jnp.split(Zs, 2, axis=1)
    Rst, Vst = jnp.split(Zst, 2, axis=1)

    bRs, bVs, bZs_dot = batching(Rs, Vs, Zs_dot,
                                size=min(len(Rs), batch_size))

    print(f"training ...")

    opt_state = opt_init(params)
    epoch = 0
    optimizer_step = -1
    larray = []
    ltarray = []
    last_loss = 1000

    start = time.time()
    train_time_arr = []
    
    for epoch in range(epochs):
        for data in zip(bRs, bVs, bZs_dot):
            optimizer_step += 1
            opt_state, params, l_ = step(
                optimizer_step, (opt_state, params, 0), *data)
        
        if epoch % 1 == 0:
            larray += [loss_fn(params, Rs, Vs, Zs_dot)]
            ltarray += [loss_fn(params, Rst, Vst, Zst_dot)]
        if epoch % saveat == 0:
            print(f"Epoch: {epoch}/{epochs}  {larray[-1]}, {ltarray[-1]}")
            savefile(f"hnn_trained_model_{ifdrag}.dil",
                     params, metadata={"savedat": epoch})
            savefile(f"loss_array_{ifdrag}.dil",
                     (larray, ltarray), metadata={"savedat": epoch})
            if last_loss > larray[-1]:
                last_loss = larray[-1]
                savefile(f"hnn_trained_model_{ifdrag}_low.dil",
                        params, metadata={"savedat": epoch})
    
            fig, axs = panel(1, 1)
            plt.semilogy(larray, label="Training")
            plt.semilogy(ltarray, label="Test")
            plt.xlabel("Epoch")
            plt.ylabel("Loss")
            plt.legend()
            plt.savefig(_filename(f"training_loss_{ifdrag}.png"))
    
        now = time.time()
        train_time_arr.append((now - start))

    fig, axs = plt.subplots(1, 1)
    plt.semilogy(larray, label="Training")
    plt.semilogy(ltarray, label="Test")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.legend()
    plt.savefig(_filename(f"training_loss_{ifdrag}.png"))
    
    params = get_params(opt_state)
    savefile(f"trained_model_{ifdrag}.dil",
             params, metadata={"savedat": epoch})
    savefile(f"loss_array_{ifdrag}.dil",
             (larray, ltarray), metadata={"savedat": epoch})
    
    # np.savetxt("../training-time/hnn.txt", train_time_arr, delimiter = "\n")
    # np.savetxt("../training-loss/hnn-train.txt", larray, delimiter = "\n")
    # np.savetxt("../training-loss/hnn-test.txt", ltarray, delimiter = "\n")

fire.Fire(main)






