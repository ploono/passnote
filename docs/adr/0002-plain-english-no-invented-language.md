# Plain English messages, no invented language

Messages are short plain English with a one-word kind (`ask`, `nak`, `prop`…), not a compressed code, cipher or invented agent language. This project started from the idea of inventing a denser language. We measured before building it, and it doesn't pay.

- **Wording barely affects cost.** Changing it moves only 0.3–0.6% of a message's real cost, because the cost is the receiver's model turns × context size (`prototype/bus/log.md` a6 and b6).
- **Invented codes cost more tokens than plain words.** Under BPE tokenizers, `ASK` is 1 token, `x7q#` is 4, an emoji is 3 and `42 17 903` is 5 (`prototype/bus/results.md`). Every receiver would also need the codebook in its context.
- **Keep messages short for a different reason:** delivered text is re-read on every later turn. Shortness is the lever; invented syntax is not.
