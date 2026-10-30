"""PreTable: the absolute pre-table sector model (forward + checked inverse).
The research copy resolves `sm` to hue_ring/sector_model.py; here it is engine/sector_model.py.

Source: research/hue_ring/local/calibration_20261020/followup_v2/code/ring_model_v2.py
Source SHA-256: c7a05f3e558378364ca7e8506a0335b8dd4895d395226c139ff8d0edc6291a37
Verbatim AST segments only (see research/studio_engine_e0/tools/extract_engine.py).
"""
import math

import numpy as np
from scipy.optimize import brentq

from . import sector_model as sm


class PreTable:
    """Absolute interpolation on input hue, with a forward-checked inverse for every output."""
    def __init__(self,model,stock):
        self.model=model;self.S=np.asarray(stock,complex);self.k=float(model['k_coupling']);self.mix='absolute_linear';self.nodes=np.array(model['nodes_unwrapped_deg']);self.sm=sm
        kern=model['kernel'];self.s0=float(kern.get('blur_deg',0.));self.kappa=float(kern.get('blur_kappa',0.));self.kernel=kern['type'];self.gain=np.array(model.get('gain_per_sector') or [model['gain']]*24)
        self.grid=np.unique(np.r_[np.arange(0.,360.00001,.5),self.nodes%360])
        self.last_ambiguity_count=0;self.last_inverse_residual=0.
        fine=np.arange(0.,360.,.1);self.min_dhout_dhin=1.
        for r in (5.,10.,15.,20.,40.):
            q=np.unwrap(np.deg2rad(fine+np.angle(self.S_of(fine,np.full(len(fine),r)),deg=True)))*180/np.pi
            self.min_dhout_dhin=min(self.min_dhout_dhin,float(np.diff(q).min()/.1))
        self.monotone=self.min_dhout_dhin>0
    def kernel_weights(self,h,cmag=None):
        h=np.atleast_1d(h);cm=np.full(len(h),20.) if cmag is None else np.broadcast_to(cmag,h.shape)
        sigma=np.sqrt(self.s0**2+(self.kappa/np.maximum(cm,1e-12))**2)
        W=np.zeros((len(h),24));small=sigma<=30.
        if small.any():W[small]=sm.weights(h[small],self.nodes,self.kernel,np.maximum(sigma[small],1e-4))
        if (~small).any():
            # Exact periodic Gaussian convolution in Fourier form for broad low-chroma distributions.
            n=self.nodes;a=np.r_[n[-1]-360,n[:-1]];c=np.r_[n[1:],n[0]+360]
            mode=np.arange(1,33);om=2*np.pi*mode/360
            if self.kernel=='linear':
                coef=-(np.exp(-1j*om[:,None]*a)/(n-a)-np.exp(-1j*om[:,None]*n)*(1/(n-a)+1/(c-n))+np.exp(-1j*om[:,None]*c)/(c-n))/(360*om[:,None]**2)
                dc=(c-a)/720
            else:
                b=np.r_[n[1:],n[0]+360];coef=(np.exp(-1j*om[:,None]*n)-np.exp(-1j*om[:,None]*b))/(360*1j*om[:,None]);dc=(b-n)/360
            phase=np.exp(1j*h[~small,None]*om)*np.exp(-.5*(sigma[~small,None]*om)**2)
            W[~small]=dc[None,:]+2*np.real(phase@coef)
        W=np.maximum(W,0.);return W/np.maximum(W.sum(1,keepdims=True),1e-12)
    def S_of(self,h,cmag=None):return self.kernel_weights(h,cmag)@self.S
    def stock_out_hue(self,c_in,cmag=None):return sm.hue_deg(c_in)
    def z(self,Zrow,h,cmag=None,Wg=None):
        if Zrow is None:return np.ones(np.shape(np.atleast_1d(h)),complex)
        W=self.kernel_weights(h,cmag);T=W@self.S;delta=(W*self.gain[None,:])@(self.S*(sm.effective(Zrow,self.k)-1));return 1+delta/T
    def forward_stock(self,c_in):
        h=sm.hue_deg(c_in);return c_in*self.S_of(h,abs(c_in)),h
    def forward(self,c_in,Zrow=None):
        cb,h=self.forward_stock(c_in);return cb*self.z(Zrow,h,abs(c_in)),cb,h
    def _radius(self,h,rout):
        h=np.atleast_1d(h);ro=np.broadcast_to(rout,h.shape);r=ro/1.8
        for _ in range(35):
            nxt=ro/np.abs(self.S_of(h,r))
            if np.max(np.abs(nxt-r)/np.maximum(nxt,1e-12))<2e-13:return nxt
            r=nxt
        # Narrow scalar root solve for the few cases whose radial fixed point is slow.
        rr=[]
        for hh,oo in zip(h,ro):
            rr.append(brentq(lambda x:x*abs(self.S_of([hh],[x])[0])-oo,max(oo/4,1e-14),max(oo*2,1e-13),xtol=1e-13))
        return np.array(rr)
    def inverse_stock(self,c_out,cmag=None,h_hint=None):
        c=np.atleast_1d(c_out).astype(complex);q=sm.hue_deg(c);h=[];radii=[];ambiguous=0;hint=None if h_hint is None else np.broadcast_to(h_hint,q.shape)
        for i,qq in enumerate(q):
            ro=abs(c[i])
            if ro<1e-10:h.append(qq);radii.append(0.);continue
            rg=self._radius(self.grid,ro);qg=np.unwrap(np.deg2rad(self.grid+np.angle(self.S_of(self.grid,rg),deg=True)))*180/np.pi
            roots=[]
            def fn(x):
                r=self._radius([x],ro)[0];return float(sm.wrap180(x+np.angle(self.S_of([x],[r])[0],deg=True)-qq))
            for shift in (-360,0,360):
                delta=qg-(qq+shift)
                for j in np.where(delta[:-1]*delta[1:]<=0)[0]:
                    lo,hi=float(self.grid[j]),float(self.grid[j+1]);fl,fh=fn(lo),fn(hi)
                    if abs(fl)<1e-10:rr=lo%360
                    elif abs(fh)<1e-10:rr=hi%360
                    else:rr=brentq(fn,lo,hi,xtol=1e-12)%360
                    if not roots or min(abs(sm.wrap180(rr-v)) for v in roots)>1e-6:roots.append(rr)
            if hint is not None:
                guide=float(hint[i])%360
                if abs(fn(guide))<1e-10:
                    roots.append(guide)
                else:
                    local=guide+np.linspace(-2.,2.,81);lr=self._radius(local,ro);lv=sm.wrap180(local+np.angle(self.S_of(local,lr),deg=True)-qq)
                    for j in np.where(lv[:-1]*lv[1:]<=0)[0]:
                        lo,hi=float(local[j]),float(local[j+1]);fl,fh=fn(lo),fn(hi)
                        root=lo if abs(fl)<1e-10 else hi if abs(fh)<1e-10 else brentq(fn,lo,hi,xtol=1e-12)
                        if not roots or min(abs(sm.wrap180(root-z)) for z in roots)>1e-6:roots.append(root%360)
            if not roots:raise ValueError(f'no stock inverse root for output hue {qq}')
            if len(roots)>1:ambiguous+=1
            guide=qq if hint is None else hint[i];chosen=min(roots,key=lambda z:abs(sm.wrap180(z-guide)));h.append(chosen);radii.append(self._radius([chosen],ro)[0])
        hh=np.array(h);ci=np.array(radii)*np.exp(1j*np.deg2rad(hh));cout,_=self.forward_stock(ci)
        err=float(np.max(abs(cout-c)))
        if err>1e-8:raise ValueError(f'stock inverse failed forward check: {err}')
        self.last_ambiguity_count=ambiguous;self.last_inverse_residual=err;return ci,hh
    def describe(self):return {'coordinate':'pre-table BT.601 CbCr hue after stock curve and type-28 matrix','interpolation':'absolute complex coefficients before stock/probe division','min_dhout_dhin_calibration_chroma':self.min_dhout_dhin,'stock_output_map_monotone':self.monotone,'inverse':'all bracketed angular roots with radial consistency and a checked forward residual; optional DNG input-hue hint'}
