"""CMD Bridge - pośrednik między powłoką systemową (CMD) a modelem AI.

Aplikacja udostępnia trwałe sesje powłoki przez lokalne API HTTP oraz przez
katalog wymiany plików (mailbox), dzięki czemu AI bez bezpośredniego dostępu
do CMD może wysyłać polecenia, czytać odpowiedzi i startować nowe sesje.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
