from config import AttackConfig
from transformers import BertTokenizer
import torch


def perturb(data, Seq2Seq_model, gan_gen, gan_adv, baseline_model, dir,
            attack_vocab):
    # Turn on evaluation mode which disables dropout.
    Seq2Seq_model.eval()
    gan_gen.eval()
    gan_adv.eval()
    baseline_model.eval()
    with torch.no_grad():
        attack_num = 0
        attack_succeeded_idx_num = torch.zeros(
            AttackConfig.perturb_search_times, AttackConfig.perturb_sample_num)
        with open(dir, "a") as f:
            for x, x_mask, y, label in data:
                x, x_mask, y, label = x.to(
                    AttackConfig.train_device), x_mask.to(
                        AttackConfig.train_device), y.to(
                            AttackConfig.train_device), label.to(
                                AttackConfig.train_device)
                tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
                # c: [batch, sen_len, hidden_size]
                c = Seq2Seq_model(x, x_mask, is_noise=False, encode_only=True)
                # z: [batch, seq_len, super_hidden_size]
                z = gan_adv(c)

                if AttackConfig.baseline_model == 'Bert':
                    x_type = torch.zeros(y.shape, dtype=torch.int64).to(
                        AttackConfig.train_device)
                    skiped = label != baseline_model(y, x_type,
                                                     x_mask).argmax(dim=1)
                else:
                    skiped = label != baseline_model(y).argmax(dim=1)
                for i in range(len(y)):
                    if skiped[i].item():
                        continue

                    attack_num += 1
                    presearch_result = [False
                                        ] * AttackConfig.perturb_sample_num

                    for t in range(AttackConfig.perturb_search_times):
                        if t == 0:
                            search_bound = 0
                        else:
                            search_bound = AttackConfig.perturb_search_bound * (
                                AttackConfig.perturb_mul**(t - 1))
                        perturb_x, presearch_result = search_fast(
                            Seq2Seq_model,
                            gan_gen,
                            baseline_model,
                            label[i],
                            z[i],
                            samples_num=AttackConfig.perturb_sample_num,
                            search_bound=search_bound,
                            presearch_result=presearch_result)

                        if attack_vocab:
                            f.write(
                                "==================================================\n"
                            )
                            f.write(f'search_bound = {search_bound}\n')
                            f.write('Orginal sentence: \n')
                            f.write(' '.join([
                                attack_vocab.get_word(token) for token in y[i]
                            ]) + "\n" * 2)

                            f.write('\nAll attack samples as follows: \n')
                            for n, perturb_x_sample in enumerate(perturb_x):
                                f.write(' '.join([
                                    attack_vocab.get_word(token)
                                    for token in perturb_x_sample
                                ]))
                                if presearch_result[n]:
                                    attack_succeeded_idx_num[t][n] += 1
                                    f.write('    attact successed!')
                                else:
                                    f.write('    attact failed!')
                                f.write('\n')
                            f.write(
                                '\n============================================\n'
                            )
                            f.flush()
                        else:
                            f.write(
                                "==================================================\n"
                            )
                            f.write(f'search_bound = {search_bound}\n')
                            f.write('Orginal sentence: \n')
                            f.write(' '.join(
                                tokenizer.convert_ids_to_tokens(y[i])) +
                                    "\n" * 2)

                            f.write('\nAll attack samples as follows: \n')
                            for n, perturb_x_sample in enumerate(perturb_x):
                                f.write(' '.join(
                                    tokenizer.convert_ids_to_tokens(
                                        perturb_x_sample)))
                                if presearch_result[n]:
                                    attack_succeeded_idx_num[t][n] += 1
                                    f.write('    attact successed!')
                                else:
                                    f.write('    attact failed!')
                                f.write('\n')
                            f.write(
                                '\n============================================\n'
                            )
                            f.flush()

    return attack_succeeded_idx_num / attack_num


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

        delta = delta.to(AttackConfig.train_device)
        search_z += delta
        # pertub_hidden: [samples_num, sen_len, hidden_size]
        perturb_hidden = generator(search_z)
        # pertub_x: [samples_num, seq_len]
        perturb_x = Seq2Seq_model.decode(perturb_hidden).argmax(dim=2)
        if AttackConfig.baseline_model == 'Bert':
            perturb_x_mask = torch.ones(perturb_x.shape, dtype=torch.int64)
            # mask before [SEP]
            for i in range(perturb_x.shape[0]):
                for word_idx in range(perturb_x.shape[1]):
                    if perturb_x[i][word_idx].item() == 102:
                        perturb_x_mask[i][word_idx + 1:] = 0
                        break
            perturb_x_mask = perturb_x_mask.to(AttackConfig.train_device)
            perturb_x_type = torch.zeros(perturb_x.shape,
                                         dtype=torch.int64).to(
                                             AttackConfig.train_device)
            # perturb_label: [samples_num]
            perturb_label = baseline_model(perturb_x, perturb_x_type,
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
