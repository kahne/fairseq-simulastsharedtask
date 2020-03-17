sudo docker build -t iwslt2020_simulast:latest .
sudo docker run --env CHKPT_FILENAME=checkpoint_text_waitk3.pt -v "$(pwd)"/experiments:/fairseq/experiments -it iwslt2020_simulast
