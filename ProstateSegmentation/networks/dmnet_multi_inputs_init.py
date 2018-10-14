"""gluon-style implementation of dmnet"""
from mxnet import nd, initializer, gpu, symbol, viz
from mxnet.gluon.nn import Conv3D, Conv3DTranspose, Activation, BatchNorm, HybridSequential, \
    HybridBlock, Dropout, MaxPool3D, InstanceNorm, LeakyReLU
from math import floor
import inspect
import time


class Init:
    """initialize parameters"""
    def __init__(self, units=[6, 12, 24, 48], num_stage=4, reduction=.5, init_channels=8, growth_rate=4,
                 bottle_neck=True, drop_out=.0, bn_mom=.9, bn_eps=1e-5, work_space=512, zKernelSize=3, zStride=1,
                 activation='relu', use_bias=False, num_fpg=8, dense_forward=False, alpha=1e-2, norm_type='batch'):
        self.units = units
        self.num_stage = num_stage
        self.growth_rate = growth_rate
        self.reduction = reduction
        self.num_fpg = num_fpg  # number of feature maps per group
        self.init_channels = init_channels
        self.bottle_neck = bottle_neck
        self.drop_out = drop_out
        self.bn_mom = bn_mom
        self.bn_eps = bn_eps
        self.work_Space = work_space
        self.zKernelSize = zKernelSize
        self.activation = activation
        self.zStride = zStride
        self.zPad = int((self.zKernelSize - zStride) / 2)
        self.use_bias = use_bias
        self.dense_forward = dense_forward
        self.alpha = alpha
        self.norm_type = norm_type

    def description(self):
        """List all parameters"""
        L = inspect.getmembers(self)
        for l in L:
            if '__' not in l[0] and l[0] != 'description':
                print('%s: %s' % (l[0], l[1]))


class FirstBlock(HybridBlock):
    """Return FirstBlock for building DenseNet"""
    def __init__(self, opts):
        super(FirstBlock, self).__init__()
        self.fblock = HybridSequential()
        self.fblock.add(Conv3D(channels=opts.init_channels, kernel_size=(opts.zKernelSize, 3, 3),
                               strides=(opts.zStride, 1, 1), padding=(opts.zPad, 1, 1), use_bias=opts.use_bias))

        # self.fblock.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
        # self.fblock.add(Activation(opts.activation))

    def hybrid_forward(self, F, x, *args, **kwargs):
        return self.fblock(x)


class BasicBlock(HybridBlock):
    """Return BaiscBlock Unit for building DenseBlock
    Parameters
    ----------
    opts: instance of Init
    """
    def __init__(self, opts):
        super(BasicBlock, self).__init__()
        self.bblock = HybridSequential()
        if opts.bottle_neck:
            if opts.norm_type is 'batch':
                self.bblock.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
            elif opts.norm_type is 'instance':
                self.bblock.add(InstanceNorm())
            if opts.activation in ['leaky']:
                self.bblock.add(LeakyReLU(alpha=opts.alpha))
            else:
                self.bblock.add(Activation(opts.activation))
            self.bblock.add(Conv3D(channels=int(opts.growth_rate * 4), kernel_size=(opts.zKernelSize, 1, 1),
                              strides=(opts.zStride, 1, 1), use_bias=opts.use_bias, padding=(opts.zPad, 0, 0)))
            if opts.drop_out > 0:
                self.bblock.add(Dropout(opts.drop_out))
        if opts.norm_type is 'batch':
            self.bblock.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
        elif opts.norm_type is 'instance':
            self.bblock.add(InstanceNorm())

        if opts.activation in ['leaky']:
            self.bblock.add(LeakyReLU(opts.alpha))
        else:
            self.bblock.add(Activation(opts.activation))
        self.bblock.add(Conv3D(channels=int(opts.growth_rate), kernel_size=(opts.zKernelSize, 3, 3),
                          strides=(opts.zStride, 1, 1), use_bias=opts.use_bias, padding=(opts.zPad, 1, 1)))
        if opts.drop_out > 0:
            self.bblock.add(Dropout(opts.drop_out))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        # print(self.bblock(x).shape)
        # print(x.shape)
        return F.Concat(x, self.bblock(x))


class DenseBlock(HybridBlock):
    """Return DenseBlock Unit for building DenseNet
    Parameters
    ----------
    opts: instance of Init
    units_num : int
        the number of BasicBlock in each DenseBlock
    """
    def __init__(self, opts, units_num):
        super(DenseBlock, self).__init__()
        self.dblock = HybridSequential()
        for i in range(units_num):
            self.dblock.add(BasicBlock(opts))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        return self.dblock(x)


class TransitionBlock(HybridBlock):
    """Return TransitionBlock Unit for building DenseNet
    Parameters
    ----------
    num_stage : int
        Number of stage
    num_filters : int
        Number of output channels
    """
    def __init__(self, opts, num_filters, pool_type='avg'):
        super(TransitionBlock, self).__init__()
        self.pool_type = pool_type
        self.tblock = HybridSequential()
        if opts.norm_type is 'batch':
            self.tblock.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
        elif opts.norm_type is 'instance':
            self.tblock.add(InstanceNorm())
        if opts.activation in ['leaky']:
            self.tblock.add(LeakyReLU(opts.alpha))
        else:
            self.tblock.add(Activation(opts.activation))
        self.tblock.add(Conv3D(channels=int(num_filters * opts.reduction), kernel_size=(opts.zKernelSize, 1, 1),
                          strides=(opts.zStride, 1, 1), use_bias=opts.use_bias, padding=(opts.zPad, 0, 0)))
        if opts.drop_out > 0:
            self.tblock.add(Dropout(opts.drop_out))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        return F.Pooling(self.tblock(x), global_pool=False, kernel=(1, 2, 2), stride=(1, 2, 2), pool_type=self.pool_type)


def conv_factory(opts, num_filters, kernel_size, stride=1, group=1):
    """A convenience function for convolution with batchnorm & activation"""
    pad = int((kernel_size - 1) / 2)
    out = HybridSequential()
    if opts.norm_type is 'batch':
        out.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
    elif opts.norm_type is 'instance':
        out.add(InstanceNorm())

    if opts.activation in ['leaky']:
        out.add(LeakyReLU(opts.alpha))
    else:
        out.add(Activation(opts.activation))

    out.add(Conv3D(channels=num_filters, kernel_size=(opts.zKernelSize, kernel_size, kernel_size),
                   strides=(opts.zStride, stride, stride), use_bias=opts.use_bias,
                   padding=(opts.zPad, pad, pad), groups=group))
    return out


class ResDBlock(HybridBlock):
    """Residual decoding block"""
    def __init__(self, opts, num_filters, group=1):
        super(ResDBlock, self).__init__()
        if opts.num_fpg != -1:
            group = int(num_filters / opts.num_fpg)
        self.body = HybridSequential()
        with self.body.name_scope():
            self.body.add(conv_factory(opts, num_filters, kernel_size=1))
            self.body.add(conv_factory(opts, num_filters, kernel_size=3, group=group))
            self.body.add(conv_factory(opts, num_filters, kernel_size=1))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        return F.concat(self.body(x), x)


class EncoderBlock_v0(HybridBlock):
    """Return a block in Encoder"""
    def __init__(self, opts, num_unit, num_filters, trans_block=True):
        super(EncoderBlock_v0, self).__init__()
        self.eblock = HybridSequential()
        if trans_block:
            self.eblock.add(TransitionBlock(opts, num_filters=num_filters))
        else:
            self.eblock.add(MaxPool3D(pool_size=(opts.zKernelSize, 3, 3),
                                      strides=(opts.zStride, 2, 2), padding=(opts.zPad, 1, 1)))
        self.eblock.add(DenseBlock(opts, num_unit))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        return self.eblock(x)


class EncoderBlock(HybridBlock):
    """Return a block in Encoder"""
    def __init__(self, opts, num_unit, num_filters, trans_block=True):
        super(EncoderBlock, self).__init__()
        self.eblock = HybridSequential()
        if trans_block:
            self.eblock.add(TransitionBlock(opts, num_filters=num_filters))
        else:
            self.eblock.add(conv_factory(opts, num_filters=8, kernel_size=1, stride=2))

        self.eblock.add(DenseBlock(opts, num_unit))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        return self.eblock(x)


class DecoderBlock(HybridBlock):
    """Return a block in Decoder"""
    def __init__(self, opts, num_filters, res_block=True, factor=1, group=1):
        super(DecoderBlock, self).__init__()
        self.dcblock = HybridSequential()
        if res_block:
            self.dcblock.add(ResDBlock(opts, num_filters * 4, group=group))
        if opts.norm_type is 'batch':
            self.dcblock.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
        elif opts.norm_type is 'instance':
            self.dcblock.add(InstanceNorm())
        if opts.activation in ['leaky']:
            self.dcblock.add(LeakyReLU(opts.alpha))
        else:
            self.dcblock.add(Activation(opts.activation))
        self.dcblock.add(Conv3DTranspose(channels=int(num_filters / factor), kernel_size=(opts.zKernelSize, 2, 2),
                                         strides=(opts.zStride, 2, 2), padding=(opts.zPad, 0, 0), use_bias=opts.use_bias))

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        out = self.dcblock(x)
        return out


class EncoderDecoderUnit(HybridBlock):
    """Return a recursive pair of encoder - decoder"""
    def __init__(self, opts, num_filters, stage, inner_block=None, innermost=False):
        super(EncoderDecoderUnit, self).__init__()

        factor = 2 if stage == 0 else 1
        encoder = EncoderBlock(opts, opts.units[stage], num_filters, trans_block=False if stage == 0 else True)
        decoder = DecoderBlock(opts, num_filters, res_block=(not innermost), factor=factor)
        if innermost:
            model = [encoder, decoder]
        else:
            model = [encoder, inner_block, decoder]

        self.net = HybridSequential()
        for block in model:
            self.net.add(block)

        if opts.dense_forward:
            self.dense_forward = HybridSequential()
            self.dense_forward.add(DenseBlock(opts, opts.units[stage]))
        else:
            self.dense_forward = None

    def hybrid_forward(self, F, x, *args, **kwargs):
        """Forward"""
        if self.dense_forward is not None:
            v = self.dense_forward(x)
            out = F.concat(v, self.net(x))
        else:
            out = F.concat(x, self.net(x))
        return out


class Softmax(HybridBlock):
    """"Softmax"""
    def __init__(self, **kwargs):
         super(Softmax, self).__init__(**kwargs)

    def hybrid_forward(self, F, x):
        """Forward"""
        return F.softmax(x, axis=1)


class DenseMultipathNet(HybridBlock):
    """Return a whole network"""
    def __init__(self, opts):
        super(DenseMultipathNet, self).__init__()
        opts.units = opts.units[:opts.num_stage]
        assert (len(opts.units) == opts.num_stage)

        num_filters = opts.init_channels
        self.num_filters_list = []
        for stage in range(opts.num_stage):
            num_filters += opts.units[stage] * opts.growth_rate
            num_filters = int(floor(num_filters * opts.reduction))
            self.num_filters_list.append(num_filters)

        self.branch1 = self.build_multi_branches()
        self.branch2 = self.build_multi_branches()
        self.notch = self.build_notch()

    def build_multi_branches(self):
        net = HybridSequential()
        with net.name_scope():
            self.blocks = EncoderDecoderUnit(opts, self.num_filters_list[opts.num_stage-1], opts.num_stage-1, innermost=True)
            for stage in range(opts.num_stage-2, -1, -1):
                self.blocks = EncoderDecoderUnit(opts, self.num_filters_list[stage], stage, inner_block=self.blocks)
            net.add(FirstBlock(opts))
            net.add(self.blocks)
            net.add(ResDBlock(opts, num_filters=16))
        return net

    def build_notch(self):
        """Summarize multiple branches"""
        net = HybridSequential()
        with net.name_scope():
            if opts.norm_type is 'batch':
                net.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
            elif opts.norm_type is 'instance':
                net.add(InstanceNorm())
            if opts.activation in ['leaky']:
                net.add(LeakyReLU(opts.alpha))
            else:
                net.add(Activation(opts.activation))
            net.add(Conv3D(kernel_size=(1, 1, 1), channels=2, use_bias=opts.use_bias))
            if opts.norm_type is 'batch':
                net.add(BatchNorm(momentum=opts.bn_mom, epsilon=opts.bn_eps))
            elif opts.norm_type is 'instance':
                net.add(InstanceNorm())
            net.add(Softmax())
        return net

    def hybrid_forward(self, F, x1, x2, *args, **kwargs):
        """Forward"""
        x3 = F.concat(self.branch1(x1), self.branch2(x2))
        return self.notch(x3)


def review_network(net, use_symbol=False, timing=True, num_rep=1, dir_out='', print_model_size=False):
    """inspect the network architecture & input - output
    use_symbol: set True to inspect the network in details
    timing: set True to estimate inference time of the network
    num_rep: number of inference"""
    # from my_func import get_model_size

    shape = (6, 4, 16, 160, 160)
    if use_symbol:
        x1 = symbol.Variable('x1')
        x2 = symbol.Variable('x2')
        y = net(x1, x2)
        if print_model_size:
            get_model_size(y, to_print=False)
        viz.plot_network(y, shape={'x1': shape, 'x2': shape}, node_attrs={"fixedsize": "false"}).view('%sDenseMultipathNet' % dir_out)
    else:
        x1 = nd.random_normal(0.1, 0.02, shape=shape, ctx=ctx)
        x2 = nd.random_normal(0.1, 0.02, shape=shape, ctx=ctx)
        net.collect_params().initialize(initializer.Xavier(magnitude=2), ctx=ctx)
        net.hybridize(static_alloc=True, static_shape=True)

        if timing:
            s1 = time.time()
            y = net(x1, x2)
            y.wait_to_read()
            print("First run: %.5f" % (time.time()-s1))

            import numpy as np
            times = np.zeros(num_rep)
            for t in range(num_rep):
                x = nd.random_normal(0.1, 0.02, shape=shape, ctx=ctx)
                s2 = time.time()
                y = net(x1, x2)
                y.wait_to_read()
                times[t] = time.time() - s2
            print("Run with hybrid network: %.5f" % times.mean())
        else:
            y = net(x)
        print("Input size: ", x.shape)
        print("Output size: ", y.shape)


if __name__ == "__main__":
    ctx = gpu()
    opts = Init(num_fpg=8)
    opts.description()

    net = DenseMultipathNet(opts)
    review_network(net, use_symbol=True)
