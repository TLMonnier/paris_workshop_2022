"""
Re-implements the investment problem using JAX.

We test

1. VFI
2. VFI with Anderson acceleration
3. HPI
4. OPI 

"""
import numpy as np
import quantecon as qe
import jax
import jax.numpy as jnp
from solvers import successive_approx
from jaxopt import FixedPointIteration, AndersonAcceleration
from investment import Model, create_investment_model

# Use 64 bit floats with JAX in order to match NumPy/Numba code
jax.config.update("jax_enable_x64", True)


def create_investment_model_jax():
    "Build a JAX-compatible version of the investment model."
    model = create_investment_model()
    β, a_0, a_1, γ, c, y_size, z_size, y_grid, z_grid, Q = model
    # Break up parameters into static and nonstatic components
    constants = β, a_0, a_1, γ, c
    sizes = y_size, z_size
    arrays = y_grid, z_grid, Q
    # Shift arrays to the device (e.g., GPU)
    arrays = tuple(map(jax.device_put, arrays))
    return constants, sizes, arrays


def B(v, constants, sizes, arrays):
    """
    A vectorized version of the right-hand side of the Bellman equation 
    (before maximization), which is a 3D array representing

        B(y, z, y′) = r(y, z, y′) + β Σ_z′ v(y′, z′) Q(z, z′)."

    for all (y, z, y′).
    
    """

    # Unpack 
    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    # Compute current rewards at all combinations of (y, z, yp)
    y  = jnp.reshape(y_grid, (y_size, 1, 1))    # y[i]   ->  y[i, j, ip]
    z  = jnp.reshape(z_grid, (1, z_size, 1))    # z[j]   ->  z[i, j, ip]
    yp = jnp.reshape(y_grid, (1, 1, y_size))    # yp[ip] -> yp[i, j, ip]
    R = (a_0 - a_1 * y + z - c) * y - γ * (yp - y)**2

    # Calculate continuation rewards at all combinations of (y, z, yp)
    v = jnp.reshape(v, (1, 1, y_size, z_size))  # v[ip, jp] -> v[i, j, ip, jp]
    Q = jnp.reshape(Q, (1, z_size, 1, z_size))  # Q[j, jp]  -> Q[i, j, ip, jp]
    C = jnp.sum(v * Q, axis=3)                  # sum over last index jp

    # Compute the right-hand side of the Bellman equation
    return R + β * C


def T(v, constants, sizes, arrays):
    "The Bellman operator."
    return jnp.max(B(v, constants, sizes, arrays), axis=2)


def get_greedy(v, constants, sizes, arrays):
    "Computes a v-greedy policy, returned as a set of indices."
    return jnp.argmax(B(v, constants, sizes, arrays), axis=2)


def T_σ(v, σ, constants, sizes, arrays):
    "The σ-policy operator."

    # Unpack model
    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    # Compute the matrix r_σ[i, j]
    y = jnp.reshape(y_grid, (y_size, 1))
    z = jnp.reshape(z_grid, (1, z_size))
    yp = y_grid[σ]
    r_σ = (a_0 - a_1 * y + z - c) * y - γ * (yp - y)**2

    yp_idx = jnp.arange(y_size)
    yp_idx = jnp.reshape(yp_idx, (1, 1, y_size, 1))
    σ = jnp.reshape(σ, (y_size, z_size, 1, 1))
    A = jnp.where(σ == yp_idx, 1, 0)
    Q = jnp.reshape(Q, (1, z_size, 1, z_size))
    P_σ = A * Q

    n = y_size * z_size
    P_σ = jnp.reshape(P_σ, (n, n))
    r_σ = jnp.reshape(r_σ, n)
    v = jnp.reshape(v, n)
    new_v = r_σ + β * P_σ @ v

    # Return as multi-index array
    return jnp.reshape(new_v, (y_size, z_size))

def T_σ_2(v, σ, constants, sizes, arrays):
    "The σ-policy operator."

    # Unpack model
    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    # Compute the matrix r_σ[i, j]
    y = jnp.reshape(y_grid, (y_size, 1))
    z = jnp.reshape(z_grid, (1, z_size))
    yp = y_grid[σ]
    r_σ = (a_0 - a_1 * y + z - c) * y - γ * (yp - y)**2

    # Compute the array v[σ[i, j], jp]
    zp_idx = jnp.arange(z_size)
    zp_idx = jnp.reshape(zp_idx, (1, 1, z_size))
    σ = jnp.reshape(σ, (y_size, z_size, 1))
    V = v[σ, zp_idx]      

    # Convert Q[j, jp] to Q[i, j, jp] 
    Q = jnp.reshape(Q, (1, z_size, z_size))

    # Calculate the expected sum Σ_jp v[σ[i, j], jp] * Q[i, j, jp]
    Ev = np.sum(V * Q, axis=2)

    return r_σ + β * np.sum(V * Q, axis=2)



def get_value(σ, constants, sizes, arrays):
    "Get the value v_σ of policy σ."

    # Unpack 
    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    y = jnp.reshape(y_grid, (y_size, 1))
    z = jnp.reshape(z_grid, (1, z_size))
    yp = y_grid[σ]
    r_σ = (a_0 - a_1 * y + z - c) * y - γ * (yp - y)**2

    yp_idx = jnp.arange(y_size)
    yp_idx = jnp.reshape(yp_idx, (1, 1, y_size, 1))
    σ = jnp.reshape(σ, (y_size, z_size, 1, 1))
    A = jnp.where(σ == yp_idx, 1, 0)
    Q = jnp.reshape(Q, (1, z_size, 1, z_size))
    P_σ = A * Q

    n = y_size * z_size
    P_σ = jnp.reshape(P_σ, (n, n))
    r_σ = jnp.reshape(r_σ, n)
    v_σ = jnp.linalg.solve(np.identity(n) - β * P_σ, r_σ)
    # Return as multi-index array
    return jnp.reshape(v_σ, (y_size, z_size))

def A(v, σ, constants, sizes, arrays):

    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    zp_idx = jnp.arange(z_size)
    zp_idx = jnp.reshape(zp_idx, (1, 1, z_size))
    σ = jnp.reshape(σ, (y_size, z_size, 1))
    V = v[σ, zp_idx]

    Q = jnp.reshape(Q, (1, z_size, z_size))

    return v - β * np.sum(V * Q, axis=2)

def get_value_2(σ, constants, sizes, arrays):
    "Get the value v_σ of policy σ."

    # Unpack 
    β, a_0, a_1, γ, c = constants
    y_size, z_size = sizes
    y_grid, z_grid, Q = arrays

    y = jnp.reshape(y_grid, (y_size, 1))
    z = jnp.reshape(z_grid, (1, z_size))
    yp = y_grid[σ]
    r_σ = (a_0 - a_1 * y + z - c) * y - γ * (yp - y)**2

    A_map = lambda v: A(v, σ, constants, sizes, arrays)

    return jax.scipy.sparse.linalg.bicgstab(A_map, r_σ)[0]


# == JIT compiled versions == #

B = jax.jit(B, static_argnums=(1, 2))
T = jax.jit(T, static_argnums=(1, 2))
get_greedy = jax.jit(get_greedy, static_argnums=(1, 2))

T_σ = jax.jit(T_σ, static_argnums=(2, 3))
T_σ_2 = jax.jit(T_σ_2, static_argnums=(2, 3))
A = jax.jit(A, static_argnums=(2, 3))

get_value = jax.jit(get_value, static_argnums=(1, 2))
get_value_2 = jax.jit(get_value_2, static_argnums=(1, 2))


# == Solvers == #

def value_iteration(model, tol=1e-5):
    "Implements VFI."

    constants, sizes, arrays = model
    _T = lambda v: T(v, constants, sizes, arrays)
    vz = jnp.zeros(sizes)

    v_star = successive_approx(_T, vz, tolerance=tol)
    return get_greedy(v_star, constants, sizes, arrays)

def policy_iteration(model):
    "Howard policy iteration routine."

    constants, sizes, arrays = model
    vz = jnp.zeros(sizes)
    σ = jnp.zeros(sizes, dtype=int)
    i, error = 0, 1.0
    while error > 0:
        v_σ = get_value_2(σ, constants, sizes, arrays)
        σ_new = get_greedy(v_σ, constants, sizes, arrays)
        error = jnp.max(np.abs(σ_new - σ))
        σ = σ_new
        i = i + 1
        print(f"Concluded loop {i} with error {error}.")
    return σ

def optimistic_policy_iteration(model, tol=1e-5, m=10):
    "Implements the OPI routine."
    constants, sizes, arrays = model
    v = jnp.zeros(sizes)
    error = tol + 1
    while error > tol:
        last_v = v
        σ = get_greedy(v, constants, sizes, arrays)
        for _ in range(m):
            v = T_σ_2(v, σ, constants, sizes, arrays)
        error = jnp.max(np.abs(v - last_v))
    return get_greedy(v, constants, sizes, arrays)


# == Tests == #

def quick_timing_test():
    model = create_investment_model_jax()
    print("Starting HPI.")
    qe.tic()
    out = policy_iteration(model)
    elapsed = qe.toc()
    print(out)
    print(f"HPI completed in {elapsed} seconds.")
    print("Starting VFI.")
    qe.tic()
    out = value_iteration(model)
    elapsed = qe.toc()
    print(out)
    print(f"VFI completed in {elapsed} seconds.")
    print("Starting OPI.")
    qe.tic()
    out = optimistic_policy_iteration(model, m=100)
    elapsed = qe.toc()
    print(out)
    print(f"OPI completed in {elapsed} seconds.")

