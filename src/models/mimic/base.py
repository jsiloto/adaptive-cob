from torch import nn

from structure.transformer import DataLogger
from models.slimmable.slimmable_ops import USConv2d, USBatchNorm2d, USConv2dStaticSamePadding

class ExtEncoder(nn.Module):
    def __init__(self, encoder, ext_classifier=None, ext_config=None):
        super().__init__()
        self.encoder = encoder
        self.ext_classifier = ext_classifier
        self.threshold = ext_config['threshold'] if ext_config is not None else None

    def forward_with_ext(self, x):
        ext_z = self.ext_classifier(x)
        if not self.training and ext_z.shape[0] == 1 and ext_z[0][1] < self.threshold:
            return None, ext_z

        z = self.encoder(x)
        return z, ext_z

    def forward(self, x):
        return self.encoder(x) if self.ext_classifier is None else self.forward_with_ext(x)

    def get_ext_classifier(self):
        return self.ext_classifier


class BottleneckBase4Ext(nn.Module):
    def __init__(self, encoder, decoder, bottleneck_transformer=None):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.bottleneck_transformer = bottleneck_transformer
        self.data_logging = isinstance(self.bottleneck_transformer, DataLogger)
        self.uses_ext_encoder = isinstance(encoder, ExtEncoder) and encoder.ext_classifier is not None
        self.use_bottleneck_transformer = False

    def forward_ext(self, z):
        z, ext_z = z
        if z is None:
            if self.data_logging:
                self.bottleneck_transformer(None, target=None)
            return z, ext_z
        elif not self.training and self.bottleneck_transformer is not None and self.use_bottleneck_transformer:
            device = z.device
            z, _ = self.bottleneck_transformer(z, target=None)
            z = z.to(device)
        return self.decoder(z), ext_z

    def forward(self, x):
        z = self.encoder(x)
        if self.uses_ext_encoder:
            return self.forward_ext(z)
        elif not self.training and self.bottleneck_transformer is not None and self.use_bottleneck_transformer:
            device = z.device
            z, _ = self.bottleneck_transformer(z, target=None)
            z = z.to(device)
        return self.decoder(z)

    def get_ext_classifier(self):
        raise NotImplementedError('get_ext_classifier function is not implemented')


class BottleneckIdentity(BottleneckBase4Ext):
    def __init__(self, config, bottleneck_transformer=None):
        encoder = nn.Sequential(
            nn.Identity()
        )
        decoder = nn.Sequential(
            nn.Identity()
        )
        super().__init__(encoder=encoder, decoder=decoder, bottleneck_transformer=bottleneck_transformer)


def set_module_width(module, width):
    if isinstance(module, (USBatchNorm2d, USConv2d, USConv2dStaticSamePadding)):
        module.width_mult = width

def set_width(module, width):
    def _set_width(m):
        set_module_width(m, width)

    module.apply(_set_width)
