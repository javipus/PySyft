from typing import Callable

import torch as th

import syft as sy
from syft.frameworks.torch.mpc.beaver import request_triple
from syft.workers.abstract import AbstractWorker
from syft.frameworks.torch.mpc.fss import remote_exec, full_name

no_wrap = {"no_wrap": True}

NAMESPACE = "syft.frameworks.torch.mpc.spdz"
authorized2 = set(f"{NAMESPACE}.{name}" for name in ["spdz_mask", "spdz_compute"])


def full_name(f):
    return f"{NAMESPACE}.{f.__name__}"


# share level
def spdz_mask(x, y, type_op):
    a, b, c = x.owner.crypto_store.get_keys(
        "beaver", op=type_op, shapes=(x.shape, y.shape), n_instances=1, remove=False
    )
    return x - a, y - b


# share level
def spdz_compute(j, delta, epsilon, type_op):
    a, b, c = delta.owner.crypto_store.get_keys(
        "beaver", op=type_op, shapes=(delta.shape, epsilon.shape), n_instances=1, remove=True
    )

    cmd = getattr(th, type_op)

    delta_b = cmd(delta, b)
    a_epsilon = cmd(a, epsilon)
    delta_epsilon = cmd(delta, epsilon)

    if j:
        return delta_epsilon + delta_b + a_epsilon + c
    else:
        return delta_b + a_epsilon + c


def spdz_mul(cmd, x, y, crypto_provider, field, dtype):
    """
    Define the workflow for a binary operation using Function Secret Sharing

    Currently supported operand are = & <=, respectively corresponding to
    type_op = 'eq' and 'comp'

    Args:
        x1: first AST
        x2: second AST
        type_op: type of operation to perform, should be 'eq' or 'comp'

    Returns:
        shares of the comparison
    """

    # TODO field
    type_op = cmd
    locations = x.locations

    shares_delta, shares_epsilon = [], []
    for location in locations:
        args = (x.child[location.id], y.child[location.id], type_op)
        share_delta, share_epsilon = remote_exec(
            full_name(spdz_mask), location, args=args, return_value=True
        )
        shares_delta.append(share_delta)
        shares_epsilon.append(share_epsilon)

    delta = sum(shares_delta)
    epsilon = sum(shares_epsilon)

    shares = []
    for i, location in enumerate(locations):
        args = (th.LongTensor([i]), delta, epsilon, type_op)
        share = remote_exec(full_name(spdz_compute), location, args=args, return_value=False)
        shares.append(share)

    shares = {loc.id: share for loc, share in zip(locations, shares)}

    response = sy.AdditiveSharingTensor(shares, **x.get_class_attributes())
    return response


def old_spdz_mul(
    cmd: Callable, x_sh, y_sh, crypto_provider: AbstractWorker, field: int, dtype: str
):
    """Abstractly multiplies two tensors (mul or matmul)	
    Args:	
        cmd: a callable of the equation to be computed (mul or matmul)	
        x_sh (AdditiveSharingTensor): the left part of the operation	
        y_sh (AdditiveSharingTensor): the right part of the operation	
        crypto_provider (AbstractWorker): an AbstractWorker which is used to generate triples	
        field (int): an integer denoting the size of the field	
        dtype (str): denotes the dtype of shares	
    Return:	
        an AdditiveSharingTensor	
    """
    assert isinstance(x_sh, sy.AdditiveSharingTensor)
    assert isinstance(y_sh, sy.AdditiveSharingTensor)

    locations = x_sh.locations
    torch_dtype = x_sh.torch_dtype

    # Get triples
    a, b, a_mul_b = request_triple(
        crypto_provider, cmd, field, dtype, x_sh.shape, y_sh.shape, locations
    )

    delta = x_sh - a
    epsilon = y_sh - b
    # Reconstruct and send to all workers
    delta = delta.reconstruct()
    epsilon = epsilon.reconstruct()

    delta_epsilon = cmd(delta, epsilon)

    # Trick to keep only one child in the MultiPointerTensor (like in SNN)
    j1 = th.ones(delta_epsilon.shape).type(torch_dtype).send(locations[0], **no_wrap)
    j0 = th.zeros(delta_epsilon.shape).type(torch_dtype).send(*locations[1:], **no_wrap)
    if len(locations) == 2:
        j = sy.MultiPointerTensor(children=[j1, j0])
    else:
        j = sy.MultiPointerTensor(children=[j1] + list(j0.child.values()))

    delta_b = cmd(delta, b)
    a_epsilon = cmd(a, epsilon)
    res = delta_epsilon * j + delta_b + a_epsilon + a_mul_b
    res = res.type(torch_dtype)
    return res
