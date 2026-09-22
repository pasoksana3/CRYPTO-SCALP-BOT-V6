CRYPTO SCALP BOT V6.5-REVERSE

This package is intentionally marked V6.5-REVERSE so it is easy to distinguish from the old V6.4 deployment.
Original V6.4 signal filters are unchanged.
Defaults: TP 1.00%, SL 0.50%, leverage 30x.
REVERSE: after an original signal, closed 5m candles are monitored. Two consecutive reverse candles plus a local break/momentum condition are required.
Reverse LONG -> SHORT and SHORT -> LONG.
Reverse Telegram message contains only direction, Futures, Entry, TP, SL and Leverage.

RAILWAY: If TP_PCT or SL_PCT exist in Variables, they override code defaults. Set TP_PCT=0.01 and SL_PCT=0.005.
