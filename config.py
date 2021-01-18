import torch


class Config:
    """configuration
    """

    train_device = torch.device('cuda:0')
    dataset_list = ['IMDB', 'AGNEWS', 'YAHOO']
    label_num = 4
    train_data_path = r'./dataset/AGNEWS/train.std'
    test_data_path = r'./dataset/AGNEWS/test.std'
    output_dir = r'./output'

    epochs = 20
    batch_size = 128

    baseline_learning_rate = 1e-4
    Seq2Seq_learning_rate = 1e-4
    gan_gen_learning_rate = 5e-5
    gan_disc_learning_rate = 1e-5
    inverter_learning_rate = 1e-5
    optim_betas = (0.9, 0.999)

    hidden_size = 768
    super_hidden_size = 500
    vocab_size = 30522
    sen_len = 20
    # new
    gan_adv_learning_rate = 1e-5
    load_pretrained_Seq2Seq = False
    fine_tuning = False


if __name__ == "__main__":
    print(Config.config_device)
