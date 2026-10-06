FROM bitcart/bitcart-xmr:0.10.3.0

ENV TON_HOST=0.0.0.0 TON_DATA_PATH=/data
LABEL org.bitcart.image=ton-daemon

COPY --chown=electrum ton.py tonaddr.py /home/electrum/site/daemons/

CMD ["just", "daemon", "ton"]
