"""D65 RGB spaces and manufacturer log equations; sources in SOURCES.md."""
import numpy as np

PRIMARIES = {
    'rec709': ((.64, .33), (.30, .60), (.15, .06)),
    'rec2020': ((.708, .292), (.170, .797), (.131, .046)),
    'vgamut': ((.730, .280), (.165, .840), (.100, -.030)),
    'sgamut3': ((.730, .280), (.140, .855), (.100, -.050)),
    'sgamut3cine': ((.766, .275), (.225, .800), (.089, -.087)),
}
GAMUT = {'srgb': 'rec709', 'rec709': 'rec709', 'gamma24': 'rec709', 'linear': 'rec709',
         'flog': 'rec2020', 'flog2': 'rec2020', 'llog': 'rec2020', 'llog709': 'rec709',
         'vlog': 'vgamut', 'slog3': 'sgamut3cine', 'slog3-sgamut3': 'sgamut3', 'applelog': 'rec2020'}
DISPLAY = ('srgb', 'rec709', 'gamma24')
SPACES = tuple(GAMUT)


def matrix(gamut):
    xy = np.asarray(PRIMARIES[gamut])
    xyz = np.stack([xy[:, 0] / xy[:, 1], np.ones(3), (1 - xy.sum(axis=1)) / xy[:, 1]])
    white = np.array([.3127/.3290, 1., (1-.3127-.3290)/.3290])
    return xyz * np.linalg.solve(xyz, white)[None, :]


def log10(x):
    # np.where evaluates both branches; the inactive logarithm must stay finite.
    return np.log10(np.maximum(x, 1e-30))


def encode(x, space):
    x = np.asarray(x, float)
    if space == 'linear':
        return x.copy()
    if space == 'srgb':
        return np.where(x <= .0031308, 12.92*x, 1.055*np.maximum(x, 0)**(1/2.4)-.055)
    if space == 'rec709':
        return np.where(x < .018, 4.5*x, 1.099*np.maximum(x, 0)**.45-.099)
    if space == 'gamma24':
        return np.sign(x)*abs(x)**(1/2.4)
    if space in ('flog', 'flog2'):
        a,b,c,d,e,f,cut = ((.555556,.009468,.344676,.790453,8.735631,.092864,.00089)
                          if space == 'flog' else (5.555556,.064829,.245281,.384316,8.799461,.092864,.000889))
        return np.where(x < cut, e*x+f, c*log10(a*x+b)+d)
    if space in ('llog', 'llog709'):
        return np.where(x <= .006, 8*x+.09, .27*log10(1.3*x+.0115)+.6)
    if space == 'vlog':
        return np.where(x < .01, 5.6*x+.125, .241514*log10(x+.00873)+.598206)
    if space in ('slog3', 'slog3-sgamut3'):
        return np.where(x < .01125, (95+x*(171.2102946929-95)/.01125)/1023,
                        (420+261.5*log10((x+.01)/.19))/1023)
    if space == 'applelog':
        r0, rt, c, beta, gamma, delta = -.05641088, .01, 47.28711236, .00964052, .08550479, .69336945
        return np.where(x >= rt, gamma*np.log2(np.maximum(x+beta, 1e-30))+delta,
                        c*np.maximum(x-r0, 0)**2)
    raise ValueError('不支持输入空间：' + space)


def decode(x, space):
    x = np.asarray(x, float)
    if space == 'linear':
        return x.copy()
    if space == 'srgb':
        return np.where(x <= .04045, x/12.92, np.maximum((x+.055)/1.055, 0)**2.4)
    if space == 'rec709':
        return np.where(x < .081, x/4.5, np.maximum((x+.099)/1.099, 0)**(1/.45))
    if space == 'gamma24':
        return np.sign(x)*abs(x)**2.4
    if space in ('flog', 'flog2'):
        a,b,c,d,e,f,cut = ((.555556,.009468,.344676,.790453,8.735631,.092864,.100537775223865)
                          if space == 'flog' else (5.555556,.064829,.245281,.384316,8.799461,.092864,.100686685370811))
        return np.where(x < cut, (x-f)/e, (10**((x-d)/c)-b)/a)
    if space in ('llog', 'llog709'):
        return np.where(x <= .1380, (x-.09)/8, (10**((x-.6)/.27)-.0115)/1.3)
    if space == 'vlog':
        return np.where(x < .181, (x-.125)/5.6, 10**((x-.598206)/.241514)-.00873)
    if space in ('slog3', 'slog3-sgamut3'):
        return np.where(x < 171.2102946929/1023, (x*1023-95)*.01125/(171.2102946929-95),
                        10**((x*1023-420)/261.5)*.19-.01)
    if space == 'applelog':
        r0, rt, c, beta, gamma, delta = -.05641088, .01, 47.28711236, .00964052, .08550479, .69336945
        return np.where(x >= c*(rt-r0)**2, 2**((x-delta)/gamma)-beta, np.sqrt(np.maximum(x, 0)/c)+r0)
    raise ValueError('不支持输出空间：' + space)


def xyz_to_encoded(xyz, space, gamut=None):
    lin = np.asarray(xyz) @ np.linalg.inv(matrix(gamut or GAMUT[space])).T
    return encode(lin, space)


def to_srgb(encoded, space='srgb', gamut=None):
    lin = decode(encoded, space)
    xyz = lin @ matrix(gamut or GAMUT[space]).T
    return encode(xyz @ np.linalg.inv(matrix('rec709')).T, 'srgb')


class Target:
    def __init__(self, cube, input_space, output_space='srgb', input_gamut=None, output_gamut=None,
                 input_range='full', output_range='full'):
        self.cube, self.input_space, self.output_space = cube, input_space, output_space
        self.input_gamut, self.output_gamut = input_gamut, output_gamut
        self.input_range, self.output_range = input_range, output_range

    def render(self, stock_rgb, xyz=None, info=False):
        if self.input_space in DISPLAY:
            lin = decode(np.asarray(stock_rgb)/255, 'srgb')
            xyz_display = lin @ matrix('rec709').T
            x = xyz_to_encoded(xyz_display, self.input_space, self.input_gamut)
        else:
            if xyz is None:
                raise ValueError('Log LUT 需要 DNG 的场景 XYZ，不能从 JPEG 猜测')
            x = xyz_to_encoded(xyz, self.input_space, self.input_gamut)
        if self.input_range == 'legal':
            x = (64+876*x)/1023
        out, stats = self.cube.apply(x, return_info=True)
        if self.output_range == 'legal':
            out = (1023*out-64)/876
        srgb = to_srgb(out, self.output_space, self.output_gamut)
        stats['display_gamut_clipped_share'] = float(np.any((srgb < 0) | (srgb > 1), axis=-1).mean())
        rgb = np.clip(srgb, 0, 1)*255
        return (rgb, stats) if info else rgb

    def description(self):
        return {'input_space': self.input_space, 'input_gamut': self.input_gamut or GAMUT[self.input_space],
                'input_range': self.input_range, 'output_space': self.output_space,
                'output_gamut': self.output_gamut or GAMUT[self.output_space], 'output_range': self.output_range,
                'metric_display': 'Rec.709 primaries, sRGB transfer, D65, 0..255',
                'exposure_matching': 'frozen DNG middle-grey anchor, no LUT-dependent auto exposure'}
