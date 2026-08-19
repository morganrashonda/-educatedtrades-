# QQQ closing imbalance to NQ overnight/opening study

Status: pre-registered before the closing-auction fields were joined to NQ
overnight and opening outcomes. Research only. No production, order, or
learning-state changes.

## Question

Does one-sided QQQ closing-auction pressure predict the next NQ overnight move
in the opposite direction, as an inventory-risk compensation mechanism would
imply? If it does, do large inventory-consistent overnight gaps fade during the
first two cash-open minutes?

The closing input is the latest QQQ Nasdaq closing-imbalance (`auction_type=C`)
message whose capture-received timestamp is at or before 15:59:50 ET. It is
joined only to the next trading session. Missing prior-session data excludes
the observation; calendar gaps are never filled.

## Frozen fields and signs

- Buy imbalance (`side=B`) is positive; sell imbalance (`side=A`) is negative.
- `signed_close_imbalance_ratio = signed total_imbalance_qty / paired_qty`.
- Inventory hypothesis direction is the opposite of the imbalance sign.
- Next-session overnight return is NQ 09:28 versus the prior 15:59 cash close.
- Opening fade return is the inherited NQ 09:30 open through 09:31 close,
  signed opposite the observed overnight gap.

The primary test uses all joined sessions and reports correlation between
signed closing imbalance and next overnight return, plus sign accuracy and
mean return aligned to the inventory hypothesis. A strong-imbalance test uses
the discovery block's median absolute imbalance ratio; this is the only new
fitted threshold.

Secondary opening tests use the already-defined absolute NQ gap of at least
1.00%. They compare inventory-consistent and inventory-inconsistent gaps. The
previous fair-value study's normalized-gap threshold is reported only as an
inherited retrospective diagnostic, not a new holdout result.

## Validation

Joined sessions are divided chronologically into discovery, validation, and
retrospective-confirmation thirds. Current-session outcomes never enter their
own feature or threshold. Any observed relationship is retrospective because
the broader opening dataset has already been inspected; an implementation
candidate still requires future shadow observations.
