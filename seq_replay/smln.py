from copy import deepcopy
import numpy as np
from scipy.sparse import csc_matrix
import time

from aux import lognormal_mu_sig, sgmd
from seq_replay import cxn
from db import make_session, d_models
from ntwk import LIFNtwk, join_w

cc = np.concatenate


def run(p, s_params, apxn):
    """
    Run smln and return rslt.
    
    :param p: dict of model params
    :param s_params: dict of smln params
    :param apxn: dict of apxn params (or None if no apxn)
    """
    # prepare smln
    prep_start = time.time()
    schedule = deepcopy(s_params['schedule'])
    
    ## build trajectory
    t = np.arange(0, schedule['SMLN_DUR'], s_params['DT'])
    trj = build_trj(t, s_params, schedule)
    
    ## adjust schedule if apxn used
    if apxn:
        schedule = fix_schedule(schedule)
        t = np.arange(0, schedule['SMLN_DUR'], s_params['DT'])
    
    ## build ntwk
    ntwk = build_ntwk(p, s_params)
    
    ## get apx. real-valued mask ("veil") over trj nrns;
    ## values are >= 0 and correspond to apx. scale factors on
    ## corresponding ST->PC weights minus 1
    trj_veil = get_trj_veil(trj, ntwk, p, s_params)
    
    ## approximate ST -> PC weights if desired
    if apxn:
        ntwk = apx_ws_up(ntwk, trj_veil)
        
    spks_up, i_ext = build_stim(t, trj, ntwk, p, s_params, schedule)
    
    prep_end = time.time()
    prep_time = prep_end - prep_start
    
    # run smln
    run_start = time.time()
    
    rslt = ntwk.run(spks_up=spks_up, dt=s_params['DT'], i_ext=i_ext)
    run_end = time.time()
    
    run_time = run_end - run_start
    
    # consolidate smln rslt
    rslt.ntwk = ntwk
    rslt.schedule = schedule
    
    rslt.p = p
    rslt.s_params = s_params
    rslt.apxn = apxn
    
    rslt.trj = trj
    rslt.trj_veil = trj_veil
    
    metrics, success = get_metrics(rslt, s_params)
    
    rslt.metrics = metrics
    rslt.success = success
    
    rslt.prep_time = prep_time
    rslt.run_time = run_time
   
    return rslt


def fix_schedule(schedule):
    """
    Update stimulus schedule to account for apxn of
    trj section by starting at beginning of replay epoch.
    """
    schedule_fixed = deepcopy(schedule)
    t_0 = schedule['REPLAY_EPOCH_START_T']
    
    schedule_fixed['REPLAY_EPOCH_START_T'] = 0
    schedule_fixed['SMLN_DUR'] = schedule['SMLN_DUR'] - t_0
    schedule_fixed['TRJ_START_T'] = schedule['TRJ_START_T'] - t_0
    schedule_fixed['TRG_START_T'] = schedule['TRG_START_T'] - t_0
    
    return schedule_fixed

 
def build_ntwk(p, s_params):
    """
    Construct a network object from the model and
    simulation params.
    """
    np.random.seed(s_params['RNG_SEED'])
    
    # set membrane properties
    n = p['N_PC'] + p['N_INH']
    
    t_m = cc(
        [np.repeat(p['T_M_PC'], p['N_PC']), np.repeat(p['T_M_INH'], p['N_INH'])])
    e_l = cc(
        [np.repeat(p['E_L_PC'], p['N_PC']), np.repeat(p['E_L_INH'], p['N_INH'])])
    v_th = cc(
        [np.repeat(p['V_TH_PC'], p['N_PC']), np.repeat(p['V_TH_INH'], p['N_INH'])])
    v_r = cc(
        [np.repeat(p['V_R_PC'], p['N_PC']), np.repeat(p['V_R_INH'], p['N_INH'])])
    t_rp = cc(
        [np.repeat(p['T_R_PC'], p['N_PC']), np.repeat(p['T_R_INH'], p['N_INH'])])
    
    # set latent nrn positions
    lb = [-s_params['BOX_W']/2, -s_params['BOX_H']/2]
    ub = [s_params['BOX_W']/2, s_params['BOX_H']/2]
    
    # sample evenly spaced place fields
    ## E cells
    pfxs_e, pfys_e = cxn.apx_lattice(lb, ub, p['N_PC'], randomize=True)
    ## I cells
    pfxs_i, pfys_i = cxn.apx_lattice(lb, ub, p['N_INH'], randomize=True)
    
    ## join E & I place fields
    pfxs = cc([pfxs_e, pfxs_i])
    pfys = cc([pfys_e, pfys_i])
    
    # make upstream ws
    if p['W_E_PC_PL'] > 0:
        w_e_pc_pl_flat = np.random.lognormal(
            *lognormal_mu_sig(p['W_E_PC_PL'], p['S_E_PC_PL']), p['N_PC'])
    else:
        w_e_pc_pl_flat = np.zeros(p['N_PC'])
    
    if p['W_E_INIT_PC_ST'] > 0:
        w_e_init_pc_st_flat = np.random.lognormal(
            *lognormal_mu_sig(p['W_E_INIT_PC_ST'], p['S_E_INIT_PC_ST']), p['N_PC'])
    else:
        w_e_init_pc_st_flat = np.zeros(p['N_PC'])
    
    ws_up_temp = {
        'E': {
            ('PC', 'PL'): np.diag(w_e_pc_pl_flat),
            ('PC', 'ST'): np.diag(w_e_init_pc_st_flat),
        },
    }
    
    targs_up = cc([np.repeat('PC', p['N_PC']), np.repeat('INH', p['N_INH'])])
    srcs_up = cc([np.repeat('PL', p['N_PC']), np.repeat('ST', p['N_PC'])])
    
    ws_up = join_w(targs_up, srcs_up, ws_up_temp)
    
    # make rcr ws
    w_e_pc_pc = cxn.make_w_e_pc_pc(pfxs[:p['N_PC']], pfys[:p['N_PC']], p)
    
    w_e_inh_pc = cxn.make_w_e_inh_pc(
        pfxs_inh=pfxs[-p['N_INH']:],
        pfys_inh=pfys[-p['N_INH']:],
        pfxs_pc=pfxs[:p['N_PC']],
        pfys_pc=pfys[:p['N_PC']],
        p=p)
    
    w_i_pc_inh = cxn.make_w_i_pc_inh(
        pfxs_pc=pfxs[:p['N_PC']],
        pfys_pc=pfys[:p['N_PC']],
        pfxs_inh=pfxs[-p['N_INH']:],
        pfys_inh=pfys[-p['N_INH']:],
        p=p)
    
    ws_rcr_temp = {
        'E': {
            ('PC', 'PC'): w_e_pc_pc,
            ('INH', 'PC'): w_e_inh_pc,
        },
        'I': {
            ('PC', 'INH'): w_i_pc_inh,
        },
    }
    targs_rcr = cc([np.repeat('PC', p['N_PC']), np.repeat('INH', p['N_INH'])])
    
    ws_rcr = join_w(targs_rcr, targs_rcr, ws_rcr_temp)
    
    # set plasticity params
    masks_plastic_temp = {
        'E': {
            ('PC', 'ST'): np.eye(p['N_PC'], dtype=bool),
        },
    }
    
    plasticity = {
        'masks': join_w(targs_up, srcs_up, masks_plastic_temp),
        'w_pc_st_maxs': {'E': p['A_P'] * w_e_init_pc_st_flat, 'I': np.array([])},
        'T_W': p['T_W'],
        'T_C': p['T_C'],
        'C_S': p['C_S'],
        'B_C': p['B_C'],
    }
    
    # make ntwk
    ntwk = LIFNtwk(
        t_m=t_m,
        e_l=e_l,
        v_th=v_th,
        v_reset=v_r,
        t_r=t_rp,
        e_ahp=p['E_AHP_PC'],
        t_ahp=p['T_AHP_PC'],
        w_ahp=p['W_AHP_PC'],
        es_syn={'E': p['E_E'], 'I': p['E_I']},
        ts_syn={'E': p['T_E'], 'I': p['T_I']},
        ws_up=ws_up,
        ws_rcr=ws_rcr,
        plasticity=plasticity)
    
    ntwk.pfxs = pfxs
    ntwk.pfys = pfys
    
    ntwk.types_up = srcs_up
    ntwk.types_rcr = targs_rcr
    
    return ntwk


def build_trj(t, s_params, schedule):
    """
    Build trajectory.
    """
    ## start
    t_0 = schedule['TRJ_START_T']
    ## first turn
    t_1 = t_0 + np.abs(s_params['TURN_X'] - s_params['START_X']) / s_params['SPEED']
    ## second turn
    t_2 = t_1 + np.abs(s_params['TURN_Y'] - s_params['START_Y']) / s_params['SPEED']
    ## end
    t_3 = t_2 + np.abs(s_params['END_X'] - s_params['TURN_X']) / s_params['SPEED']
    
    x = np.repeat(np.nan, len(t))
    y = np.repeat(np.nan, len(t))
    sp = np.repeat(np.nan, len(t))
    
    # before first leg
    x[t < t_0] = s_params['START_X']
    y[t < t_0] = s_params['START_Y']
    sp[t < t_0] = 0
    
    # first leg (horizontal 1)
    mask_0 = (t_0 <= t) & (t < t_1)
    x[mask_0] = np.linspace(s_params['START_X'], s_params['TURN_X'], mask_0.sum())
    y[mask_0] = s_params['START_Y']
    sp[mask_0] = s_params['SPEED']
    
    # second leg (vertical)
    mask_1 = (t_1 <= t) & (t < t_2)
    x[mask_1] = s_params['TURN_X']
    y[mask_1] = np.linspace(s_params['START_Y'], s_params['TURN_Y'], mask_1.sum())
    sp[mask_1] = s_params['SPEED']
    
    # third leg (horizontal 2)
    mask_2 = (t_2 <= t) & (t < t_3)
    x[mask_2] = np.linspace(s_params['TURN_X'], s_params['END_X'], mask_2.sum())
    y[mask_2] = s_params['TURN_Y']
    sp[mask_2] = s_params['SPEED']
    
    # after third leg
    x[t_3 <= t] = s_params['END_X']
    y[t_3 <= t] = s_params['TURN_Y']
    sp[t_3 <= t] = 0
    
    if np.any(np.isnan(x)) or np.any(np.isnan(y)) or np.any(np.isnan(sp)):
        raise ValueError('NaNs detected in trj.')
    
    return {'x': x, 'y': y, 'sp': sp}
 
    
def get_trj_veil(trj, ntwk, p, s_params):
    """
    Return a "veil" (positive real-valued mask) over cells in the ntwk
    with place fields along the trajectory path.
    """
    # compute scale factor for all PCs
    ## get distance to trj
    d = dist_to_trj(ntwk.pfxs, ntwk.pfys, trj['x'], trj['y'])[0]
    
    ## compute scale factor
    radius = s_params['metrics']['RADIUS']
    pitch = s_params['metrics']['PITCH']
    g = np.maximum(1 - np.abs(d/radius)**pitch, 0)
    veil = ((1 - g)*1 + g*p['A_P']) - 1
    
    return veil
    
    
def dist_to_trj(pfxs, pfys, x, y):
    """
    Compute distance of static points (pfxs, pfys) to trajectory (x(t), y(t)).
    
    :return: dists to nearest pts, idxs of nearest pts
    """
    # get dists to all pts along trj
    dx = np.tile(pfxs[None, :], (len(x), 1)) - np.tile(x[:, None], (1, len(pfxs)))
    dy = np.tile(pfys[None, :], (len(y), 1)) - np.tile(y[:, None], (1, len(pfys)))
    
    d = np.sqrt(dx**2 + dy**2)
    
    # return dists of cells to nearest pts on trj
    return np.min(d, 0), np.argmin(d, 0)

   
def apx_ws_up(ntwk, trj_veil):
    """
    Replace ST->PC E weights with apxns expected following
    initial sensory input.
    """
    scale = trj_veil[ntwk.types_rcr == 'PC'] + 1
    
    ws_up_init_e_dense = np.array(ntwk.ws_up_init['E'].todense())
    ws_up_init_e_dense[ntwk.plasticity['masks']['E']] *= scale
    
    ntwk.ws_up_init['E'] = csc_matrix(ws_up_init_e_dense)
    
    return ntwk

       
def build_stim(t, trj, ntwk, p, s_params, schedule):
    """
    Put together upstream spk and external current inputs
    according to stimulation params and schedule.
    """
    np.random.seed(s_params['RNG_SEED'])
    
    # initialize upstream spks array
    spks_up = np.zeros((len(t), 2*p['N_PC']), int)
    
    # fill in trajectory spks if required
    if schedule['REPLAY_EPOCH_START_T'] > 0:
        spks_up += spks_up_from_trj(trj, ntwk, p, s_params)
    
    # fill in replay epoch STATE inputs
    spks_up += spks_up_from_st(t, ntwk, p, s_params, schedule)
    
    # initialize external current array
    i_ext = np.zeros((len(t), p['N_PC'] + p['N_INH']))
    
    # add replay trigger
    i_ext += i_ext_trg(t, ntwk, p, s_params, schedule)
    
    return spks_up, i_ext


def spks_up_from_trj(trj, ntwk, p, s_params):
    """
    Generate trajectory-driven upstream spk rates.
    """
    n_t = len(trj['x'])
    
    # get spk rates
    
    ## dists from x, y to place fields
    x_ = np.tile(trj['x'][:, None], (1, p['N_PC']))
    y_ = np.tile(trj['y'][:, None], (1, p['N_PC']))
    
    pfxs_ = np.tile(ntwk.pfxs[None, :p['N_PC']], (n_t, 1))
    pfys_ = np.tile(ntwk.pfys[None, :p['N_PC']], (n_t, 1))
    
    d = np.sqrt((x_ - pfxs_)**2 + (y_ - pfys_)**2)
    
    ## dist-dependent rates
    rs_d = p['R_MAX'] * np.exp(-np.abs(d**2)/(2*p['L_PL']**2))
    
    ## speed-modulation
    sp_ = np.tile(trj['sp'][:, None], (1, p['N_PC']))
    fs = sgmd((sp_ - p['S_TH'])/p['B_S'])
    
    ## get spk rates
    spk_rs = rs_d * fs
    
    # get spks
    spks_tmp = np.random.poisson(spk_rs * s_params['DT'])
    
    # convert to full-sized input upstream input array
    return cc([spks_tmp, np.zeros(spks_tmp.shape, int)], 1)


def spks_up_from_st(t, ntwk, p, s_params, schedule):
    """
    Add ST --> PC spks to upstream spk array.
    """
    spks_up = np.zeros((len(t), 2*p['N_PC']), int)
    
    # sens/traj epoch
    if schedule['REPLAY_EPOCH_START_T'] > 0:
        mask = t <= schedule['REPLAY_EPOCH_START_T']
        spks_up[mask, -p['N_PC']:] += np.random.poisson(
            p['FR_TRJ_PC_ST'] * s_params['DT'], (mask.sum(), p['N_PC']))
        
    # replay epoch
    mask = schedule['REPLAY_EPOCH_START_T'] < t
    spks_up[-mask.sum():, p['N_PC']:] += np.random.poisson(
        p['FR_RPL_PC_ST'] * s_params['DT'], (mask.sum(), p['N_PC']))
        
    return spks_up


def i_ext_trg(t, ntwk, p, s_params, schedule):
    """
    Add replay trigger to external current stim.
    """
    i_ext = np.zeros((len(t), p['N_PC'] + p['N_INH']))
    
    # get mask over cells to trigger to induce replay
    ## compute distances to trigger center
    trg_mask = get_trg_mask_pc(ntwk, p, s_params)
    
    ## get time mask
    t_mask = (schedule['TRG_START_T'] <= t) \
        & (t < (schedule['TRG_START_T'] + p['D_T_TR']))
    
    ## add in external trigger
    i_ext[np.outer(t_mask, trg_mask)] = p['A_TR']
    
    return i_ext


def get_trg_mask_pc(ntwk, p, s_params):
    dx = ntwk.pfxs - s_params['X_TRG']
    dy = ntwk.pfys - s_params['Y_TRG']
    d = np.sqrt(dx**2 + dy**2)
    
    ## get mask
    trg_mask = (d < p['R_TR']) & (ntwk.types_rcr == 'PC')
    return trg_mask


def get_metrics(rslt, s_params):
    """
    Compute basic metrics from smln rslt quantifying replay properties.
    """
    m = s_params['metrics']
    mask_pc = rslt.ntwk.types_rcr == 'PC'
    
    # get masks over trj & non-trj PCs
    trj_mask = (rslt.trj_veil * mask_pc) > (m['MIN_SCALE_TRJ'] - 1)
    non_trj_mask = (~trj_mask) & mask_pc
    
    # get t_mask for detection window
    start = rslt.schedule['TRG_START_T']
    end = start + m['WDW']
    t_mask = (start <= rslt.ts) & (rslt.ts < end)
    
    # get spk cts for trj/non-trj cells during detection window
    spks_wdw = rslt.spks[t_mask]
    spks_trj = spks_wdw[:, trj_mask]
    spks_non_trj = spks_wdw[:, non_trj_mask]
    
    # get fraction of trj/non-trj cells that spiked
    frac_spk_trj = np.mean(spks_trj.sum(0) > 0)
    frac_spk_non_trj = np.mean(spks_non_trj.sum(0) > 0)
    
    # get avg spk ct of spiking trj cells
    avg_spk_ct_trj = spks_trj.sum(0)[spks_trj.sum(0) > 0].mean()
    
    # check conditions for successful replay 
    if (frac_spk_trj >= m['MIN_FRAC_SPK_TRJ']) \
            and (frac_spk_trj >= (frac_spk_non_trj*m['TRJ_NON_TRJ_SPK_RATIO'])) \
            and (avg_spk_ct_trj < m['MAX_AVG_SPK_CT_TRJ']):
        success = True
    else:
        success = False
        
    metrics = {
        'frac_spk_trj': frac_spk_trj,
        'frac_spk_non_trj': frac_spk_non_trj,
        'avg_spk_ct_trj': avg_spk_ct_trj,
        'success': success,
    }
    
    return metrics, success


def save(rslt, group, commit):
    session = make_session()
    
    smln_rslt = d_models.SmlnRslt(
        group=group,
        
        params=rslt.p,
        s_params=rslt.s_params,
        apxn=rslt.apxn,
        
        metrics=rslt.metrics,
        success=rslt.success,
        
        prep_time=rslt.prep_time,
        run_time=rslt.run_time,
        
        ntwk_file='',
        smln_included=False,
        
        commit=commit)
    
    session.add(smln_rslt)
    session.commit()
    
    session.close()
    
    return smln_rslt
