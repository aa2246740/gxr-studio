"""Deterministic integer reference. All transforms restart at immutable base bytes.

Doses are signed Q12 values, not UI levels. The calibration table maps each
UI level to a dose. No firmware, file, settings, RPC or device I/O lives here.
"""
import struct
import zlib
from dataclasses import dataclass

Q=4096
EXP_Q12=(4096, 4277, 4467, 4664, 4871, 5087, 5312, 5547, 5793, 6049, 6317, 6597, 6889, 7194, 7512, 7845, 8192, 8555, 8933, 9329, 9742, 10173, 10624, 11094, 11585, 12098, 12634, 13193, 13777, 14387, 15024, 15689, 16384, 17109, 17867, 18658, 19484, 20347, 21247, 22188, 23170, 24196, 25268, 26386, 27554, 28774, 30048, 31379, 32768, 34219, 35734, 37316, 38968, 40693, 42495, 44376, 46341, 48393, 50535, 52773, 55109, 57549, 60097, 62757, 65536, 68438, 71468, 74632, 77936, 81386, 84990, 88752, 92682, 96785, 101070, 105545, 110218, 115098, 120194, 125515, 131072, 136875, 142935, 149263, 155872, 162773, 169979, 177505, 185364, 193571, 202141, 211090, 220436, 230195, 240387, 251030, 262144, 273750, 285870, 298527, 311744, 325546, 339959, 355010, 370728, 387141, 404281, 422180, 440872, 460391, 480774, 502060, 524288, 547500, 571740, 597053, 623487, 651091, 679917, 710020, 741455, 774282, 808563, 844361, 881744, 920782, 961548, 1004120, 1048576, 1095000, 1143480, 1194106, 1246974, 1302182, 1359835, 1420040, 1482910, 1548564, 1617125, 1688722, 1763488, 1841564, 1923097, 2008240, 2097152, 2190001, 2286960, 2388213, 2493948, 2604365, 2719670, 2840080, 2965821, 3097129, 3234251, 3377443, 3526975, 3683128, 3846194, 4016479, 4194304, 4380002, 4573921, 4776426, 4987896, 5208729, 5439339, 5680159, 5931642, 6194258, 6468501, 6754886, 7053950, 7366255, 7692387, 8032959, 8388608, 8760003, 9147842, 9552851, 9975792, 10417458, 10878679, 11360319, 11863283, 12388516, 12937002, 13509772, 14107901, 14732511, 15384775, 16065917, 16777216, 17520007, 18295684, 19105703, 19951585, 20834917, 21757357, 22720638, 23726566, 24777031, 25874004, 27019544, 28215802, 29465022, 30769550, 32131834, 33554432, 35040014, 36591368, 38211406, 39903169, 41669834, 43514715, 45441276, 47453133, 49554062, 51748008, 54039088, 56431603, 58930044, 61539100, 64263668, 67108864, 70080027, 73182735, 76422812, 79806339, 83339667, 87029429, 90882551, 94906266, 99108125, 103496017, 108078177, 112863206, 117860087, 123078199, 128527337, 134217728, 140160054, 146365470, 152845623, 159612677, 166679334, 174058859, 181765103, 189812531, 198216250, 206992033, 216156353, 225726413, 235720175, 246156398, 257054673, 268435456)
MAX_DOSES=(16384,16384,16384,4096,16384,4096,4096,4096,4096,4096)
PARAMS=('contrast','highlight','shadow','fade','saturation','warmth','tint','split','filter','tone')
OFFSETS=(20,82,115,216,284,346,379,412,456,518)
LENGTHS=(62,33,96,56,62,33,33,33,62,33)
CRCS=((20,62,8),(82,33,12),(115,96,16),(216,56,212),(284,62,272),
      (346,33,276),(379,66,280),(456,62,448),(518,33,452))


def rd(n,d):
    if d<=0: raise ValueError('positive divisor')
    return (n+d//2)//d if n>=0 else -((-n+d//2)//d)


def clamp(v,lo,hi,stats=None,key='clamps'):
    r=max(lo,min(hi,v))
    if stats is not None and r!=v: stats[key]+=1
    return r


def dec(v,sign=2048): return -(v&(sign-1)) if v&sign else v&(sign-1)
def enc(v,sign=2048): return sign|(-v) if v<0 else v
def words(b,at=216): return [dec(v) for v in struct.unpack_from('>28H',b,at)]


@dataclass(frozen=True)
class Request:
    amount: int=100
    doses: tuple=(0,)*10
    wb_method: int=0
    tone_method: int=0

    def validate(self):
        if type(self.amount) is not int or not 0<=self.amount<=150: raise ValueError('amount')
        if len(self.doses)!=10 or any(type(x) is not int or not -limit<=x<=limit for x,limit in zip(self.doses,MAX_DOSES)): raise ValueError('doses')
        if self.doses[3]<0: raise ValueError('fade is one-way')
        if self.wb_method not in (0,1) or self.tone_method not in (0,1): raise ValueError('method')


def checksum(b):
    for at,length,crc_at in CRCS: struct.pack_into('>I',b,crc_at,zlib.crc32(b[at:at+length]))


def effective(b,stock):
    o=bytearray(b)
    if not b[1]&1:o[216:272]=stock[216:272]
    if not b[1]&2:o[379:445]=b[82:115]*2
    if not b[1]&16:o[284:379]=stock[284:379]
    return o


def interp_q16(a,curve,x):
    xs=(0,*struct.unpack('>31H',a),4095)
    if x>=4095:return curve[32]*65536
    right=1
    while xs[right]<x:right+=1
    left=right-1
    return curve[left]*65536+rd((x-xs[left])*(curve[right]-curve[left])*65536,xs[right]-xs[left])


def blend(stock,base,amount,stats):
    if amount==100:return bytearray(base)
    if amount==0 or base==stock:return bytearray(stock)
    a=effective(stock,stock);b=effective(base,stock);out=bytearray(base)
    out[1]=stock[1]|base[1]
    if out[1]&4:struct.pack_into('>H',out,445,0)
    for at,length in zip(OFFSETS,LENGTHS):
        if at in (379,412) and not out[1]&2:continue
        if length==62:
            out[at:at+62]=b[at:at+62]
        elif length==96:
            for j in range(length):
                v=rd((100-amount)*dec(a[at+j],128)+amount*dec(b[at+j],128),100)
                out[at+j]=enc(clamp(v,-127,127,stats),128)
            for j in range(0,96,4):
                if a[at+j+2]==(a[at+j+1]^128) and a[at+j+3]==a[at+j] and b[at+j+2]==(b[at+j+1]^128) and b[at+j+3]==b[at+j]:
                    out[at+j+2]=out[at+j+1]^128;out[at+j+3]=out[at+j]
        elif length==56:
            for j in range(28):
                av=struct.unpack_from('>H',a,at+2*j)[0];bv=struct.unpack_from('>H',b,at+2*j)[0]
                if j%14<9:
                    v=rd((100-amount)*dec(av)+amount*dec(bv),100);lim=2047 if j%14<3 else 1279
                    v=enc(clamp(v,-lim,lim,stats))
                else:v=clamp(rd((100-amount)*av+amount*bv,100),0,4095,stats)
                struct.pack_into('>H',out,at+2*j,v)
        else:
            prev=0;aa=20 if at in (82,379,412) else 284 if at==346 else 456
            xs=(0,*struct.unpack_from('>31H',b,aa),4095)
            for j in range(length):
                av=interp_q16(a[aa:aa+62],a[at:at+33],xs[j])
                v=rd((100-amount)*av+amount*b[at+j]*65536,100*65536)
                v=clamp(v,0,255,stats)
                if v<prev:stats['monotone_repairs']+=1;v=prev
                out[at+j]=v;prev=v
    return out


def limit_slopes(b,stats):
    pairs=[(20,82),(456,518)]
    if b[1]&16:pairs.append((284,346))
    if b[1]&4:pairs.extend(((20,379),(20,412)))
    for aa,bb in pairs:
        xs=(0,*struct.unpack_from('>31H',b,aa),4095)
        for j in range(1,33):
            cap=b[bb+j-1]+xs[j]-xs[j-1]
            if b[bb+j]>cap:b[bb+j]=cap;stats['slope_repairs']+=1


def warp(y,lo,hi,s):
    if not s or y<=lo or y>=hi:return y
    t=y-lo;length=hi-lo
    j,rem=divmod(abs(s),64)
    gain=EXP_Q12[j] if j==256 else EXP_Q12[j]+rd((EXP_Q12[j+1]-EXP_Q12[j])*rem,64)
    n,d=(gain,Q) if s>0 else (Q,gain)
    return lo+rd(length*n*t,d*(length-t)+n*t)


def tone_curve(curve,doses):
    c,h,s,f=doses[:4];out=[]
    for y in curve:
        if c:y=warp(y,0,128,-c) if y<=128 else warp(y,128,255,c)
        if h:y=warp(y,112,255,h)
        if s:y=warp(y,0,144,s)
        if f:
            black=rd(abs(f)*80,Q)
            y=rd(black*255+(255-black)*y,255)
        out.append(y)
    return bytes(out)


def sat(b,dose,stats):
    if not dose:return
    w=words(b);idx=[i for i in range(28) if 3<=i%14<9]
    wanted=Q+3*dose if dose>0 else rd(Q*Q,Q+3*abs(dose))
    cm=max(v&127 for v in b[115:211]);mm=max(abs(w[i]) for i in idx)
    cg=min(wanted,127*Q//cm if cm else wanted)
    residual=rd(wanted*Q,cg)
    mg=min(residual,1279*Q//mm if mm else residual)
    stats['colour_cap']+=int(cg<wanted);stats['matrix_cap']+=int(mg<residual)
    for j in range(96):
        old=b[115+j];b[115+j]=(old&128)|rd((old&127)*cg,Q)
    if mg!=Q:
        for i in idx:w[i]=(1 if w[i]>=0 else -1)*rd(abs(w[i])*mg,Q)
        store_matrix(b,w)


def store_matrix(b,w):
    struct.pack_into('>28H',b,216,*(enc(v) for v in w))
    b[1]|=1


def add_rowsum(b,cb,cr,stats):
    if not cb and not cr:return
    w=words(b)
    deltas=[0]*28
    for half in (0,14):
        y=w[half:half+3];ys=sum(y)
        if ys<=0:raise ValueError('positive Y sum required')
        for row,amount in ((3,cb),(6,cr)):
            dd=[rd(amount*v,ys) for v in y]
            j=max(range(3),key=lambda k:abs(y[k]));dd[j]+=amount-sum(dd)
            for k in range(3):deltas[half+row+k]=dd[k]
    alpha=Q
    for i,d in enumerate(deltas):
        if d>0:alpha=min(alpha,(1279-w[i])*Q//d)
        elif d<0:alpha=min(alpha,(-1279-w[i])*Q//d)
    alpha=max(0,min(Q,alpha));stats['rowsum_cap']+=int(alpha<Q)
    for i,d in enumerate(deltas):w[i]+=rd(d*alpha,Q)
    store_matrix(b,w)


def filter_weights(y,dose):
    if not dose:return list(y)
    if dose<0:
        a,b,t,den=y,(96,896,32),-dose,Q
    else:
        anchors=(tuple(y),(420,580,24),(700,300,24),(930,80,14))
        p=dose*3;seg=min(2,p//Q);a,b=anchors[seg:seg+2];t=p-seg*Q;den=Q
    out=[rd((den-t)*av+t*bv,den) for av,bv in zip(a,b)]
    j=max(range(3),key=lambda k:out[k]);out[j]+=1024-sum(out)
    return out


def bw_filter(b,dose):
    if not dose:return
    w=words(b)
    for half in (0,14):w[half:half+3]=filter_weights(w[half:half+3],dose)
    store_matrix(b,w)


def t1(b,warmth,tint,split,stats):
    if not (warmth or tint or split):return
    if not b[1]&2:b[379:445]=b[82:115]*2
    b[1]|=6;struct.pack_into('>H',b,445,0)
    for at,sign in ((379,1),(412,-1)):
        for j in range(33):
            y=b[at+j]
            gain=Q+rd(3*abs(warmth),5)
            if warmth*sign>0:y=rd(y*gain,Q)
            elif warmth*sign<0:y=rd(y*Q,gain)
            gain=Q+rd(abs(tint),2)
            if tint>0:y=rd(y*gain,Q)
            elif tint<0:y=rd(y*Q,gain)
            y=clamp(y,0,255,stats)
            if split:y=warp(y,0,128,-sign*split) if y<=128 else warp(y,128,255,sign*split)
            b[at+j]=clamp(y,0,255,stats)


def apply(stock,base,request=Request()):
    request.validate()
    if len(stock)!=552 or len(base)!=552:raise ValueError('entry length')
    stats={k:0 for k in ('clamps','node_repairs','monotone_repairs','slope_repairs','colour_cap','matrix_cap','rowsum_cap')}
    doses=request.doses
    if request.amount==100 and not any(doses):return bytes(base),stats
    b=blend(stock,base,request.amount,stats)
    if not b[1]&1:b[216:272]=stock[216:272]
    # Every active absolute curve receives the same monotone output mapping.
    if any(doses[:4]):
        for at in (82,518):b[at:at+33]=tone_curve(b[at:at+33],doses)
        if b[1]&16:b[346:379]=tone_curve(b[346:379],doses)
        if b[1]&2:
            for at in (379,412):b[at:at+33]=tone_curve(b[at:at+33],doses)
    if doses[4]:sat(b,doses[4],stats)
    if doses[8]:bw_filter(b,doses[8])
    if request.wb_method==0:
        add_rowsum(b,rd(-140*doses[5]+100*doses[6],Q),rd(180*doses[5]+110*doses[6],Q),stats)
        warm=tint=0
    else:warm,tint=doses[5:7]
    if doses[9]:
        if request.tone_method==0:add_rowsum(b,rd(-128*doses[9],Q),rd(128*doses[9],Q),stats)
        else:
            w=words(b)
            for half in (0,14):
                w[half+10]=clamp(w[half+10]+rd(-8*doses[9],Q),120,136,stats)
                w[half+11]=clamp(w[half+11]+rd(8*doses[9],Q),120,136,stats)
            store_matrix(b,w)
    t1(b,warm,tint,doses[7],stats)
    limit_slopes(b,stats)
    checksum(b)
    return bytes(b),stats


def request_for(param,dose,amount=100,wb_method=0,tone_method=0):
    ds=[0]*10
    if param!='amount':ds[PARAMS.index(param)]=dose
    return Request(amount,tuple(ds),wb_method,tone_method)
