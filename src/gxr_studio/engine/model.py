"""Host prediction (电脑预测): 552 B entry + white-balanced sensor codes -> sRGB.
Model.pre / predict / lab / de and masks / synthetic / recipe_chart are verbatim from model_adapter.py;
only the constructor is new (research: closed_loop Engine from DEEPTBL0.DAT + repo paths; here: data from camdata).

Source: research/knobs_v2_r2/src/model_adapter.py
Source SHA-256: 368e6c99523fdccfb7c608f3e257cff82946e69fb6d38658ff239a5177f4a0a0
Verbatim AST segments only (see research/studio_engine_e0/tools/extract_engine.py).
"""
from types import SimpleNamespace

import numpy as np

from . import colour, matrix_model
from . import knobs as k
from .pretable import PreTable


class Model:
    """The A12-50 ISO200/1600 calibrated chain. For qlaunch/l08firm it is a transferred model, not a calibration.

    stock_words: (24, 2) ints, the sector words of the A12-50 (jlaunch) original 04 entry, decoded with
    colour.decode_word. d0/d1: the five-colour modifier (24 each). ring_model: the gxr-sector-model/2 dict.
    """

    def __init__(self, stock_words, d0, d1, ring_model):
        if ring_model.get('schema') != 'gxr-sector-model/2':
            raise ValueError('需要 v2 pre-table 模型')
        stock = np.asarray(stock_words, dtype=np.int64)
        d0, d1 = np.asarray(d0, dtype=float), np.asarray(d1, dtype=float)
        if stock.shape != (24, 2) or d0.shape != (24,) or d1.shape != (24,):
            raise ValueError('stock/d0/d1 shape')
        S_eff = ((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32
        ctx = SimpleNamespace(stock=stock, d0=d0, d1=d1, S_eff=S_eff)
        self.engine = SimpleNamespace(mm=matrix_model, fs=colour, ctx=ctx, table=PreTable(ring_model, S_eff))
        self.mm, self.fs = matrix_model, colour

    def pre(self,b,x,channel_mode=None):
        xs=np.array([0,*__import__('struct').unpack_from('>31H',b,20),4095])
        active=bool(b[1]&4) if channel_mode is None else bool(channel_mode)
        curves=(b[379:412],b[82:115],b[412:445]) if active else (b[82:115],)*3
        return np.column_stack([np.interp(np.clip(x[:,j],0,4095),xs,list(curves[j])) for j in range(3)])

    def predict(self,b,x,channel_mode=None,quantize=False,half=0,bw_chroma='conditional'):
        pre=self.pre(b,x,channel_mode)
        row=self.mm.MatrixRow.from_bytes(b[216+half*28:244+half*28])
        # The B&W entry stores +0/+0/+0/+0. Negative zero is numerically zero;
        # fs.decode_word insists on a bitwise conjugate encoding and rejects it.
        coeff=np.array([[k.dec(z,128) for z in b[i:i+4]] for i in range(115,211,4)])
        if np.any(coeff[:,0]!=coeff[:,3]) or np.any(coeff[:,1]!=-coeff[:,2]):
            raise ValueError('frozen symmetric sector model cannot predict asymmetric rows')
        ab=coeff[:,:2]
        z=self.fs.Z_of_words(ab[:,0],ab[:,1],self.engine.ctx)
        y,ci=row.apply(pre)
        if b[0]==3:
            # Engine.predict('03') always discards chroma. For Tone only, this
            # explicit hypothesis lets matrix chroma reach BT.601 directly.
            # Its visibility is CONDITIONAL, never a camera or Engine proof.
            co=ci if bw_chroma=='conditional' else np.zeros_like(ci)
        else:co=self.engine.table.forward(ci,z)[0]
        rgb=np.clip(self.mm.rgb_from_ycc(y,co),0,255)
        return np.floor(rgb+.5).astype(np.uint8) if quantize else rgb

    def lab(self,rgb):return self.fs.lab_from_codes(rgb)
    def de(self,a,b):return self.fs.delta_e00(a,b)


def synthetic():
    # Dense neutral ramp and a deterministic linear-light RGB cube, no photograph selection by result.
    neutral=np.repeat(np.geomspace(3,4095,257)[:,None],3,axis=1)
    grid=np.geomspace(12,3500,9)
    rgb=np.array(np.meshgrid(grid,grid,grid,indexing='ij')).reshape(3,-1).T
    return np.vstack((neutral,rgb))


def recipe_chart(model,base):
    # Equal areas per baseline output code avoid overweighting nearly black
    # patches merely because a logarithmic sensor grid contains many of them.
    raw=np.repeat(np.arange(4096)[:,None],3,axis=1)
    y=model.mm.ycbcr(model.predict(base,raw))[0]
    yy=np.maximum.accumulate(y);keep=np.r_[True,np.diff(yy)>1e-9]
    targets=np.linspace(max(1,float(yy[0])),min(254,float(yy[-1])),257)
    neutral=np.repeat(np.interp(targets,yy[keep],np.arange(4096)[keep])[:,None],3,axis=1)
    return np.vstack((neutral,synthetic()[257:]))


def masks(model,base,x,param,chart=False):
    rgb=model.predict(base,x,quantize=True);lab=model.lab(rgb)
    y=model.mm.ycbcr(rgb)[0];chroma=np.hypot(lab[:,1],lab[:,2])
    masks={'contrast':(y>=15)&(y<=240),'highlight':(y>=155)&(y<=245),
           'shadow':(y>=12)&(y<=110),'fade':(y>=5)&(y<=80),
           'saturation':(chroma>=15)&(y>=15)&(y<=235),
           'warmth':(chroma<=15)&(y>=35)&(y<=220),
           'tint':(chroma<=15)&(y>=35)&(y<=220),
           'filter':(chroma>=0)&(np.max(x,axis=1)-np.min(x,axis=1)>80)&(y>=15)&(y<=235),
           'tone':(y>=35)&(y<=220),'amount':(y>=15)&(y<=235)}
    neutral=np.max(x,axis=1)-np.min(x,axis=1)<1e-8
    if param=='split':
        result=[(y>=20)&(y<=90),(y>=160)&(y<=235)]
        return [m&neutral for m in result] if chart else result
    result=masks[param]
    if chart and param in ('contrast','highlight','shadow','fade'):result=result&neutral
    return [result]
