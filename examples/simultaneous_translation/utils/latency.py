import torch
class LatencyMetric(object):
    @staticmethod
    def length_from_padding_mask(padding_mask, batch_first: bool = False):
        dim = 1 if batch_first else 0
        return padding_mask.size(dim) - padding_mask.sum(dim=dim, keepdim=True)

    def prepare_latency_metric(
        self,
        delays,
        src_lens,
        target_padding_mask=None,
        batch_first: bool = False,
        start_from_zero: bool = True
    ):
        assert len(delays.size()) == 2
        assert len(src_lens.size()) == 2

        if start_from_zero:
            delays = delays + 1

        if batch_first:
            # convert to batch_last
            delays = delays.t()
            src_lens = src_lens.t()
            tgt_len, bsz = delays.size()
            _, bsz_1 = src_lens.size()

            if target_padding_mask is not None:
                target_padding_mask = target_padding_mask.t()
                tgt_len_1, bsz_2 = target_padding_mask.size()
                assert tgt_len == tgt_len_1
                assert bsz == bsz_2

        assert bsz == bsz_1

        if target_padding_mask is None:
            tgt_lens = tgt_len * delays.new_ones([1, bsz]).float()
        else:
            # 1, batch_size
            tgt_lens = self.length_from_padding_mask(target_padding_mask, False).float()
            delays = delays.masked_fill(target_padding_mask, 0)

        return delays, src_lens, tgt_lens, target_padding_mask

    def __call__(
        self,
        delays,
        src_lens,
        target_padding_mask=None,
        batch_first: bool = False,
        start_from_zero: bool = True,
    ):
        delays, src_lens, tgt_lens, target_padding_mask = self.prepare_latency_metric(
            delays,
            src_lens,
            target_padding_mask,
            batch_first,
            start_from_zero
        )
        return self.cal_metric(delays, src_lens, tgt_lens, target_padding_mask)

    @staticmethod
    def cal_metric(delays, src_lens, tgt_lens, target_padding_mask):
        """
        Expected sizes:
        delays: tgt_len, batch_size
        src_lens: 1, batch_size
        target_padding_mask: tgt_len, batch_size
        """
        raise NotImplementedError


class AverageProportion(LatencyMetric):
    """
    Function to calculate Average Proportion from
    Can neural machine translation do simultaneous translation?
    (https://arxiv.org/abs/1606.02012)

    Delays are monotonic steps, range from 1 to src_len.
    Give src x tgt y, AP is calculated as:

    AP = 1 / (|x||y]) sum_i^|Y| deleys_i
    """
    @staticmethod
    def cal_metric(delays, src_lens, tgt_lens, target_padding_mask):
        if target_padding_mask is not None:
            AP = torch.sum(delays.masked_fill(target_padding_mask, 0), dim=0, keepdim=True)
        else:
            AP = torch.sum(delays, dim=0, keepdim=True)

        AP = AP / (src_lens * tgt_lens)
        return AP


class AverageLagging(LatencyMetric):
    """
    Function to calculate Average Lagging from
    STACL: Simultaneous Translation with Implicit Anticipation
    and Controllable Latency using Prefix-to-Prefix Framework
    (https://arxiv.org/abs/1810.08398)

    Delays are monotonic steps, range from 1 to src_len.
    Give src x tgt y, AP is calculated as:

    AL = 1 / tau sum_i^tau delays_i - (i - 1) / gamma

    Where
    gamma = |y| / |x|
    tau = argmin_i(delays_i = |x|)
    """
    @staticmethod
    def cal_metric(delays, src_lens, tgt_lens, target_padding_mask):
        # tau = argmin_i(delays_i = |x|)
        tgt_len, bsz = delays.size()
        lagging_padding_mask = delays >= src_lens
        lagging_padding_mask = torch.nn.functional.pad(lagging_padding_mask.t(), (1, 0)).t()[:-1, :]
        gamma = tgt_lens / src_lens
        lagging = delays - torch.arange(delays.size(0)).unsqueeze(1).type_as(delays).expand_as(delays) / gamma
        lagging.masked_fill_(lagging_padding_mask, 0)
        tau = (1 - lagging_padding_mask.type_as(lagging)).sum(dim=0, keepdim=True)
        AL = lagging.sum(dim=0, keepdim=True) / tau

        return AL


class DifferentiableAverageLagging(LatencyMetric):
    """
    Function to calculate Differentiable Average Lagging from
    Monotonic Infinite Lookback Attention for Simultaneous Machine Translation
    (https://arxiv.org/abs/1906.05218)

    Delays are monotonic steps, range from 0 to src_len-1.
    (In the original paper thery are from 1 to src_len)
    Give src x tgt y, AP is calculated as:

    DAL = 1 / |Y| sum_i^|Y| delays'_i - (i - 1) / gamma

    Where
    delays'_i =
        1. delays_i if i == 1
        2. max(delays_i, delays'_{i-1} + 1 / gamma)

    """
    @staticmethod
    def cal_metric(delays, src_lens, tgt_lens, target_padding_mask):
        tgt_len, bsz = delays.size()

        gamma = tgt_lens / src_lens
        new_delays = torch.zeros_like(delays)

        for i in range(delays.size(0)):
            if i == 0:
                new_delays[i] = delays[i]
            else:
                new_delays[i] = torch.cat(
                    [
                        new_delays[i - 1].unsqueeze(0) + 1 / gamma,
                        delays[i].unsqueeze(0)
                    ],
                    dim=0
                ).max(dim=0)[0]

        DAL = (
            new_delays - torch.arange(delays.size(0)).unsqueeze(1).type_as(delays).expand_as(delays) / gamma
        )
        if target_padding_mask is not None:
            DAL = DAL.masked_fill(target_padding_mask, 0)

        DAL = DAL.sum(dim=0, keepdim=True) / tgt_lens

        return DAL


class LatencyInference(object):
    def __init__(self, start_from_zero=True):
        self.metric_calculator = {
            "differentiable_average_lagging": DifferentiableAverageLagging(),
            "average_lagging": AverageLagging(),
            "average_proportion": AverageProportion(),
        }

        self.start_from_zero = start_from_zero

    def __call__(self, monotonic_step, src_lens):
        """
        monotonic_step range from 0 to src_len. src_len means eos
        delays: bsz, tgt_len
        src_lens: bsz, 1
        """
        if not self.start_from_zero:
            monotonic_step -= 1
        
        src_lens = src_lens

        delays = (
            monotonic_step
            .view(monotonic_step.size(0), -1, monotonic_step.size(-1))
            .max(dim=1)[0]
        )

        delays = (
            delays.masked_fill(delays >= src_lens, 0)
            + (src_lens - 1)
            .expand_as(delays)
            .masked_fill(delays < src_lens, 0)
        )
        return_dict = {}
        for key, func in self.metric_calculator.items():
            return_dict[key] = func(
                delays.float(), src_lens.float(),
                target_padding_mask=None,
                batch_first=True,
                start_from_zero=True
            ).t()

        return return_dict