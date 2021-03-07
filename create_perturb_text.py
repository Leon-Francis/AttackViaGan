from transformers import BertTokenizer
from model import Seq2Seq_bert, LSTM_G, LSTM_A
from baseline_module.baseline_model_builder import BaselineModelBuilder
from train import build_dataset
from config import AttackConfig
from calc_BertScore_ppl import calc_bert_score_ppl
from tools import logging
import torch
import os


def perturb(data, Seq2Seq_model, gan_gen, gan_adv, baseline_model, cands_dir,
            refs_dir, attack_vocab, search_bound, samples_num):
    # Turn on evaluation mode which disables dropout.
    Seq2Seq_model.eval()
    gan_gen.eval()
    gan_adv.eval()
    baseline_model.eval()
    with torch.no_grad():
        attack_num = 0
        attack_success_num = 0
        with open(cands_dir, "w") as f, open(refs_dir, "w") as f_1:
            for x, x_mask, y, label in data:
                x, x_mask, y, label = x.to(train_device), x_mask.to(
                    train_device), y.to(train_device), label.to(train_device)
                tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
                # c: [batch, sen_len, hidden_size]
                c = Seq2Seq_model(x, x_mask, is_noise=False, encode_only=True)
                # z: [batch, seq_len, super_hidden_size]
                z = gan_adv(c)

                if baseline_model == 'Bert':
                    skiped = label != baseline_model(y, x_mask).argmax(dim=1)
                else:
                    skiped = label != baseline_model(y).argmax(dim=1)
                for i in range(len(y)):
                    if skiped[i].item():
                        continue

                    attack_num += 1
                    presearch_result = [False] * samples_num

                    perturb_x, presearch_result = search_fast(
                        Seq2Seq_model,
                        gan_gen,
                        baseline_model,
                        label[i],
                        z[i],
                        samples_num=samples_num,
                        search_bound=search_bound,
                        presearch_result=presearch_result)

                    if attack_vocab:
                        for stop in range(len(y[i]), 0, -1):
                            if y[i][stop - 1].item() != 0:
                                break
                        f_1.write(' '.join([
                            attack_vocab.get_word(token)
                            for token in y[i][:stop]
                        ]) + "\n")

                        if not presearch_result[-1]:
                            for stop in range(len(perturb_x[0]), 0, -1):
                                if perturb_x[0][stop - 1].item() != 0:
                                    break
                            f.write(' '.join([
                                attack_vocab.get_word(token)
                                for token in perturb_x[0][:stop]
                            ]))
                            f.write('\n')
                        else:
                            for n, perturb_x_sample in enumerate(perturb_x):
                                if presearch_result[n]:
                                    for stop in range(len(perturb_x_sample), 0,
                                                      -1):
                                        if perturb_x_sample[stop -
                                                            1].item() != 0:
                                            break
                                    f.write(' '.join([
                                        attack_vocab.get_word(token)
                                        for token in perturb_x_sample[:stop]
                                    ]))
                                    f.write('\n')
                                    attack_success_num += 1
                                    break

                    else:
                        for stop in range(len(y[i]), 0, -1):
                            if y[i][stop - 1].item() != 0:
                                break
                        f_1.write(' '.join(
                            tokenizer.convert_ids_to_tokens(y[i][:stop])) +
                                  "\n")

                        if not presearch_result[-1]:
                            for stop in range(len(perturb_x[0]), 0, -1):
                                if perturb_x[0][stop - 1].item() != 0:
                                    break
                            f.write(' '.join(
                                tokenizer.convert_ids_to_tokens(perturb_x[0])))
                            f.write('\n')
                        else:
                            for n, perturb_x_sample in enumerate(perturb_x):
                                if presearch_result[n]:
                                    for stop in range(len(perturb_x_sample), 0,
                                                      -1):
                                        if perturb_x_sample[stop -
                                                            1].item() != 0:
                                            break
                                    f.write(' '.join(
                                        tokenizer.convert_ids_to_tokens(
                                            perturb_x_sample)))
                                    f.write('\n')
                                    attack_success_num += 1
                                    break

    return attack_success_num / attack_num


def search_fast(Seq2Seq_model, generator, baseline_model, label, z,
                samples_num, search_bound, presearch_result):
    # z: [sen_len, super_hidden_size]
    Seq2Seq_model.eval()
    generator.eval()
    baseline_model.eval()
    with torch.no_grad():

        # search_z: [samples_num, sen_len, super_hidden_size]
        search_z = z.repeat(samples_num, 1, 1)
        delta = torch.FloatTensor(search_z.size()).uniform_(
            -1 * search_bound, search_bound)

        delta = delta.to(train_device)
        search_z += delta
        # pertub_hidden: [samples_num, sen_len, hidden_size]
        perturb_hidden = generator(search_z)
        # pertub_x: [samples_num, seq_len]
        perturb_x = Seq2Seq_model.decode(perturb_hidden).argmax(dim=2)
        if baseline_model == 'Bert':
            perturb_x_mask = torch.ones(perturb_x.shape)
            # mask before [SEP]
            for i in range(perturb_x.shape[0]):
                for word_idx in range(perturb_x.shape[1]):
                    if perturb_x[i][word_idx].item() == 102:
                        perturb_x_mask[i][word_idx + 1:] = 0
                        break
            perturb_x_mask = perturb_x_mask.to(train_device)
            # perturb_label: [samples_num]
            perturb_label = baseline_model(perturb_x,
                                           perturb_x_mask).argmax(dim=1)
        else:
            perturb_label = baseline_model(perturb_x).argmax(dim=1)

        successed_mask = perturb_label != label
        for i in range(len(presearch_result)):
            if not presearch_result[i]:
                for t in range(i + 1):
                    if successed_mask[t].item():
                        presearch_result[i] = True
                        break

    return perturb_x, presearch_result


if __name__ == '__main__':
    train_device = torch.device('cuda:1')
    dataset = 'AGNEWS'
    baseline_model = 'LSTM'
    search_bound = 0.1
    samples_num = 100

    cur_dir = './output/gan_model/AGNEWS/LSTM/1614521151/models/epoch39/'  # gan_adv gan_gen Seq2Seq_model
    output_dir = f'./texts/OUR/{dataset}/{baseline_model}'
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    refs_dir = output_dir + f'/refs_{search_bound}_{samples_num}.txt'
    cands_dir = output_dir + f'/cands_{search_bound}_{samples_num}.txt'

    baseline_model_builder = BaselineModelBuilder(dataset,
                                                  baseline_model,
                                                  train_device,
                                                  is_load=True)

    Seq2Seq_model = Seq2Seq_bert(
        baseline_model_builder.vocab.num).to(train_device)
    Seq2Seq_model.load_state_dict(
        torch.load(cur_dir + 'Seq2Seq_model.pt', map_location=train_device))

    gan_gen = LSTM_G(AttackConfig.super_hidden_size,
                     AttackConfig.hidden_size,
                     num_layers=3).to(train_device)
    gan_gen.load_state_dict(
        torch.load(cur_dir + 'gan_gen.pt', map_location=train_device))

    gan_adv = LSTM_A(AttackConfig.hidden_size,
                     AttackConfig.super_hidden_size,
                     num_layers=3).to(train_device)
    gan_adv.load_state_dict(
        torch.load(cur_dir + 'gan_adv.pt', map_location=train_device))

    _, test_data = build_dataset(baseline_model_builder.vocab)

    attack_acc = perturb(test_data, Seq2Seq_model, gan_gen, gan_adv,
                         baseline_model_builder.net, cands_dir, refs_dir,
                         baseline_model_builder.vocab, search_bound,
                         samples_num)
    logging(f'attack_acc={attack_acc}')

    ppl, bert_score = calc_bert_score_ppl(cands_dir, refs_dir)
    logging(f'ppl={ppl}')
    logging(f'bert_score={bert_score}')