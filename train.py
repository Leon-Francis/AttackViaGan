import os
import time
from model import Seq2Seq_bert, LSTM_G, LSTM_A
from baseline_model import Baseline_Model_Bert
from data import Seq2Seq_DataSet
from tools import logging
from config import Config
from torch import nn, optim
from torch.utils.data import DataLoader
import torch
from transformers import BertTokenizer
from perturb import perturb
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', choices=config_dataset_list)
parser.add_argument('--model', choices=config_model_lists)
# parser.add_argument('--save_acc_limit', help='set a acc lower limit for saving model',
#                     type=float, default=0.85)
parser.add_argument('--epoch', type=int, default=20)
parser.add_argument('--enhanced', type=parse_bool, choices=[True, False])
parser.add_argument('--adv',
                    choices=[True, False],
                    default='no',
                    type=parse_bool)
parser.add_argument('--batch', type=int, default=64)
parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--note', type=str, default='')
parser.add_argument('--load_model',
                    choices=[True, False],
                    default='no',
                    type=parse_bool)
parser.add_argument('--verbose',
                    choices=[True, False],
                    default='no',
                    type=parse_bool)
args = parser.parse_args()


def train_Seq2Seq(train_data, model, criterion, optimizer, total_loss):
    model.train()
    x, x_mask, y, _ = train_data
    x, x_mask, y = x.to(Config.train_device), x_mask.to(
        Config.train_device), y.to(Config.train_device)
    logits = model(x, x_mask, is_noise=False)
    optimizer.zero_grad()
    logits = logits.reshape(-1, logits.shape[-1])
    y = y.reshape(-1)
    loss = criterion(logits, y)
    loss.backward()
    optimizer.step()

    total_loss += loss.item()

    return total_loss


def train_gan_a(train_data, Seq2Seq_model, gan_gen, gan_adv, baseline_model,
                optimizer_gan_a, criterion_ce):
    gan_gen.train()
    gan_adv.train()
    optimizer_gan_a.zero_grad()

    x, x_mask, y, label = train_data
    # perturb_x: [batch, sen_len]
    perturb_x = Seq2Seq_model(x,
                              x_mask,
                              is_noise=False,
                              generator=gan_gen,
                              adversary=gan_adv).argmax(dim=2)
    # perturb_x_mask: [batch, seq_len]
    perturb_x_mask = torch.ones(perturb_x.shape, requires_grad=True)
    with torch.no_grad():
        # mask before [SEP]
        for i in range(perturb_x.shape[0]):
            for word_idx in range(perturb_x.shape[1]):
                if perturb_x[i][word_idx].item() == 102:
                    perturb_x_mask[i][word_idx + 1:] = 0
                    break
    perturb_x_mask = perturb_x_mask.to(Config.train_device)
    # perturb_logits: [batch, 4]
    perturb_logits = baseline_model(perturb_x, perturb_x_mask)

    loss = criterion_ce(perturb_logits, label)
    loss *= -1
    loss.backward()
    optimizer_gan_a.step()

    return -loss.item()


def train_gan_g(train_data, Seq2Seq_model, gan_gen, gan_adv, criterion_mse,
                optimizer_gan_g, optimizer_gan_a):
    gan_gen.train()
    gan_adv.train()
    optimizer_gan_g.zero_grad()
    optimizer_gan_a.zero_grad()

    x, x_mask, y, _ = train_data
    # real_hidden: [batch, sen_len, hidden]
    real_hidden = Seq2Seq_model(x, x_mask, is_noise=False, encode_only=True)
    if Config.gan_gen_train_model:
        # perturb_x: [batch, sen_len]
        perturb_x = Seq2Seq_model(x,
                                  x_mask,
                                  is_noise=False,
                                  generator=gan_gen,
                                  adversary=gan_adv).argmax(dim=2)
        # perturb_x_mask: [batch, seq_len]
        perturb_x_mask = torch.ones(perturb_x.shape, requires_grad=True)
        with torch.no_grad():
            # mask before [SEP]
            for i in range(perturb_x.shape[0]):
                for word_idx in range(perturb_x.shape[1]):
                    if perturb_x[i][word_idx].item() == 102:
                        perturb_x_mask[i][word_idx + 1:] = 0
                        break
        perturb_x_mask = perturb_x_mask.to(Config.train_device)
        fake_hidden = Seq2Seq_model(perturb_x,
                                    perturb_x_mask,
                                    is_noise=False,
                                    encode_only=True)
    else:
        # fake_hidden: [batch, sen_len, hidden]
        fake_hidden = gan_gen(gan_adv(real_hidden))

    loss = criterion_mse(real_hidden.reshape(real_hidden.shape[0], -1),
                         fake_hidden.reshape(fake_hidden.shape[0], -1))

    loss.backward()
    optimizer_gan_g.step()
    optimizer_gan_a.step()

    return loss.item()


def evaluate_gan(test_data, Seq2Seq_model, gan_gen, gan_adv, dir):
    Seq2Seq_model.eval()
    gan_gen.eval()
    gan_adv.eval()
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    logging(f'Saving evaluate of gan outputs into {dir}')
    with torch.no_grad():

        for x, x_mask, y, _ in test_data:
            x, x_mask, y = x.to(Config.train_device), x_mask.to(
                Config.train_device), y.to(Config.train_device)

            # sentence -> encoder -> decoder
            Seq2Seq_outputs = Seq2Seq_model(x, x_mask, is_noise=False)
            # Seq2Seq_idx: [batch, seq_len]
            Seq2Seq_idx = Seq2Seq_outputs.argmax(dim=2)

            # sentence -> encoder -> adversary -> generator ->  decoder
            # eagd_outputs: [batch, seq_len, vocab_size]
            eagd_outputs = Seq2Seq_model(x,
                                         x_mask,
                                         is_noise=False,
                                         generator=gan_gen,
                                         adversary=gan_adv)
            # eagd_idx: [batch_size, sen_len]
            eagd_idx = eagd_outputs.argmax(dim=2)

            with open(dir, 'a') as f:
                for i in range(len(y)):
                    f.write('------orginal sentence---------\n')
                    f.write(' '.join(tokenizer.convert_ids_to_tokens(y[i])) +
                            '\n')
                    f.write('------setence -> encoder -> decoder-------\n')
                    f.write(' '.join(
                        tokenizer.convert_ids_to_tokens(Seq2Seq_idx[i])) +
                            '\n')
                    f.write(
                        '------sentence -> encoder -> inverter -> generator -> decoder-------\n'
                    )
                    f.write(' '.join(
                        tokenizer.convert_ids_to_tokens(eagd_idx[i])) +
                            '\n' * 2)


def evaluate_Seq2Seq(test_data, Seq2Seq_model, dir):
    Seq2Seq_model.eval()
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    logging(f'Saving evaluate of Seq2Seq_model outputs into {dir}')
    with torch.no_grad():
        acc_sum = 0
        n = 0
        for x, x_mask, y, _ in test_data:
            x, x_mask, y = x.to(Config.train_device), x_mask.to(
                Config.train_device), y.to(Config.train_device)
            logits = Seq2Seq_model(x, x_mask, is_noise=False)
            # outputs_idx: [batch, sen_len]
            outputs_idx = logits.argmax(dim=2)
            acc_sum += (outputs_idx == y).float().sum().item()
            n += y.shape[0] * y.shape[1]
            with open(dir, 'a') as f:
                for i in range(len(y)):
                    f.write('-------orginal sentence----------\n')
                    f.write(' '.join(tokenizer.convert_ids_to_tokens(y[i])) +
                            '\n')
                    f.write(
                        '-------sentence -> encoder -> decoder----------\n')
                    f.write(' '.join(
                        tokenizer.convert_ids_to_tokens(outputs_idx[i])) +
                            '\n' * 2)

        return acc_sum / n


def save_all_models(Seq2Seq_model, gan_gen, gan_adv, dir):
    logging('Saving models...')
    torch.save(Seq2Seq_model.state_dict(), dir + '/Seq2Seq_model.pt')
    torch.save(gan_gen.state_dict(), dir + '/gan_gen.pt')
    torch.save(gan_adv.state_dict(), dir + '/gan_adv.pt')


def save_config(dir):
    with open(dir, 'w') as f:
        f.write(f'Config.train_device:{Config.train_device}\n')
        f.write(f'Config.train_data_path:{Config.train_data_path}\n')
        f.write(f'Config.epochs:{Config.epochs}\n')
        f.write(f'Config.batch_size:{Config.batch_size}\n')
        f.write(f'Config.hidden_size:{Config.hidden_size}\n')
        f.write(f'Config.super_hidden_size:{Config.super_hidden_size}\n')
        f.write(f'Config.sen_len:{Config.sen_len}\n')
        f.write(
            f'Config.baseline_learning_rate:{Config.baseline_learning_rate}\n')
        f.write(
            f'Config.Seq2Seq_learning_rate:{Config.Seq2Seq_learning_rate}\n')
        f.write(
            f'Config.gan_gen_learning_rate:{Config.gan_gen_learning_rate}\n')
        f.write(
            f'Config.gan_adv_learning_rate:{Config.gan_adv_learning_rate}\n')
        f.write(
            f'Config.load_pretrained_Seq2Seq:{Config.load_pretrained_Seq2Seq}\n'
        )
        f.write(f'Config.fine_tuning:{Config.fine_tuning}\n')
        f.write(f'Config.gan_gen_train_model:{Config.gan_gen_train_model}\n')


if __name__ == '__main__':
    args = parser.parse_args()
    logging('Using cuda device gpu: ' + str(Config.train_device.index))
    cur_dir = Config.output_dir + '/gan_model/' + str(int(time.time()))
    cur_dir_models = cur_dir + '/models'
    # make output directory if it doesn't already exist
    if not os.path.isdir(Config.output_dir):
        os.makedirs(Config.output_dir)
    if not os.path.isdir(Config.output_dir + '/gan_model'):
        os.makedirs(Config.output_dir + '/gan_model')
    if not os.path.isdir(cur_dir):
        os.makedirs(cur_dir)
        os.makedirs(cur_dir_models)
    logging('Saving into directory' + cur_dir)
    save_config(cur_dir + '/config.log')

    # prepare dataset
    logging('preparing data...')
    train_dataset_orig = Seq2Seq_DataSet(Config.train_data_path)
    test_dataset_orig = Seq2Seq_DataSet(Config.test_data_path)
    train_data = DataLoader(train_dataset_orig,
                            batch_size=Config.batch_size,
                            shuffle=True,
                            num_workers=4)
    test_data = DataLoader(test_dataset_orig,
                           batch_size=Config.batch_size,
                           shuffle=False,
                           num_workers=4)
    logging('prepare data finished')

    # init models
    logging('init models, optimizer, criterion...')
    Seq2Seq_model_bert = Seq2Seq_bert(hidden_size=Config.hidden_size).to(
        Config.train_device)
    gan_gen = LSTM_G(Config.super_hidden_size,
                     Config.hidden_size,
                     num_layers=3).to(Config.train_device)

    gan_adv = LSTM_A(Config.hidden_size,
                     Config.super_hidden_size,
                     num_layers=3).to(Config.train_device)
    baseline_model_bert = Baseline_Model_Bert().to(Config.train_device)
    # load pretrained
    baseline_model_bert.load_state_dict(
        torch.load(
            'output/baseline_model/1610975155/models/baseline_model_bert.pt',
            map_location=Config.train_device))
    if Config.load_pretrained_Seq2Seq:
        Seq2Seq_model_bert.load_state_dict(
            torch.load(
                'output/Seq2Seq_model/1609511458/models/Seq2Seq_model_bert.pt',
                map_location=Config.train_device))

    # init optimizer
    optimizer_Seq2Seq = optim.Adam(Seq2Seq_model_bert.parameters(),
                                   lr=Config.Seq2Seq_learning_rate)
    optimizer_gan_g = optim.Adam(gan_gen.parameters(),
                                 lr=Config.gan_gen_learning_rate,
                                 betas=Config.optim_betas)
    optimizer_gan_a = optim.Adam(gan_adv.parameters(),
                                 lr=Config.gan_adv_learning_rate,
                                 betas=Config.optim_betas)
    # init criterion
    criterion_ce = nn.CrossEntropyLoss().to(Config.train_device)
    criterion_mse = nn.MSELoss().to(Config.train_device)
    logging('init models, optimizer, criterion finished')

    # start training
    logging('Training Seq2Seq Model...')

    for epoch in range(Config.epochs):
        niter = 0
        total_loss_Seq2Seq = 0
        total_loss_gan_a = 0
        total_loss_gan_g = 0
        logging(f'Training {epoch} epoch')
        for x, x_mask, y, label in train_data:
            niter += 1
            x, x_mask, y, label = x.to(Config.train_device), x_mask.to(
                Config.train_device), y.to(Config.train_device), label.to(
                    Config.train_device)

            if not Config.load_pretrained_Seq2Seq:
                for i in range(5):
                    total_loss_Seq2Seq += train_Seq2Seq(
                        (x, x_mask, y, label), Seq2Seq_model_bert,
                        criterion_ce, optimizer_Seq2Seq, total_loss_Seq2Seq)
            else:
                if Config.fine_tuning:
                    for i in range(5):
                        total_loss_Seq2Seq += train_Seq2Seq(
                            (x, x_mask, y, label), Seq2Seq_model_bert,
                            criterion_ce, optimizer_Seq2Seq,
                            total_loss_Seq2Seq)

            for i in range(10):
                total_loss_gan_g += train_gan_g(
                    (x, x_mask, y, label), Seq2Seq_model_bert, gan_gen,
                    gan_adv, criterion_mse, optimizer_gan_g, optimizer_gan_a)

            for i in range(1):
                total_loss_gan_a += train_gan_a(
                    (x, x_mask, y, label), Seq2Seq_model_bert, gan_gen,
                    gan_adv, baseline_model_bert, optimizer_gan_a,
                    criterion_ce)

            if niter % 100 == 0:
                # decaying noise
                Seq2Seq_model_bert.noise_std *= 0.995
                logging(
                    f'epoch {epoch}, niter {niter}:Loss_Seq2Seq: {total_loss_Seq2Seq / niter / Config.batch_size / 5}, Loss_gan_g: {total_loss_gan_g / niter / Config.batch_size / 5}, Loss_gan_a: {total_loss_gan_a / niter / Config.batch_size / 5}'
                )

        # end of epoch --------------------------------
        # evaluation

        logging(f'epoch {epoch} evaluate Seq2Seq model')
        Seq2Seq_acc = evaluate_Seq2Seq(
            test_data, Seq2Seq_model_bert,
            cur_dir_models + f'/epoch{epoch}_evaluate_Seq2Seq')
        logging(f'Seq2Seq_model_bert acc = {Seq2Seq_acc}')

        logging(f'epoch {epoch} evaluate gan')
        evaluate_gan(test_data, Seq2Seq_model_bert, gan_gen, gan_adv,
                     cur_dir_models + f'/epoch{epoch}_evaluate_gan')

        if (epoch + 1) % 5 == 0:
            os.makedirs(cur_dir_models + f'/epoch{epoch}')
            save_all_models(Seq2Seq_model_bert, gan_gen, gan_adv,
                            cur_dir_models + f'/epoch{epoch}')

            logging(f'epoch {epoch} Staring perturb')
            perturb(test_data, Seq2Seq_model_bert, gan_gen, gan_adv,
                    baseline_model_bert, cur_dir + f'/epoch{epoch}_perturb')

# hidden: [batch_size, sen_len, hidden_size]
# generator: LSTM
# adversary: LSTM
# Seq2Seq_model_bert train