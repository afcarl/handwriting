import numpy as np
from collections import namedtuple

import torch
import torch.nn.functional as F
from torch.autograd import Variable

# Named tuple to hold sampling resuls
# It is more concise and prevents bugs that may arise if parameters are somehow shuffled
SampleParams = namedtuple("SampleParams", ["x1", "x2", "eos", "mu1", "mu2", "sigma1", "sigma2", "rho"])

# Named tuple to hold plotting data
# It is more concise and prevents bugs that may arise if parameters are somehow shuffled
PlotData = namedtuple("PlotData", ["stroke", "window", "phi", "density", "text", "kappa"])


def sample_unconditional_sequence(settings, rnn):
    """Utility to sample an unconditional handwriting sequence from a RNN

    Strategy: Initialize sequence with 1,0,0 : i.e. end of stroke, no previously drawn data
    Pass the sequence to the RNN and obtain Mixture Gaussian parameters
    Then sample from this mixture gaussian
    and iterate until settings.sampling_len is reached

    Args:
        settings (ExperimentSettings): custom class to hold hyperparams
        rnn (nn.Model): the model we want to sample from

    Returns:
        plot_data (PlotData) custom class holding data arrays for plots
    """

    # Prepare input sequence
    sample = [1., 0., 0.]  # eos = 1 to tell the model to generate a new sample dx and dy initialized at 0
    sample = torch.Tensor(sample).view(1,1,3)  # format in (seq_len, batch_size, n_features) mode
    if settings.use_cuda:
        sample = sample.cuda()

    sample = Variable(sample)
    # Prepare hidden var
    hidden = rnn.initHidden(sample.size(1))

    list_stroke = []
    for i in range(settings.sampling_len):

        mdnparams, e_logit, hidden = rnn(sample, hidden)

        # Sample a new data point
        params = stroke_sampling(settings, mdnparams, e_logit)

        # Roll out params
        x1 = params.x1
        x2 = params.x2
        eos = params.eos

        # Redefine sample
        sample = torch.cat([eos, x1, x2], -1).view(1,1,3)

        # Add sample to the sequence
        list_stroke.append(sample.squeeze().data.cpu().numpy())

    arr_stroke = np.array(list_stroke)

    out = PlotData(stroke=arr_stroke, window=None, phi=None, density=None, text=None, kappa=None)

    return out


def sample_fixed_sequence(settings, rnn, truth_text="an input string"):
    """Utility to sample an fixed sequence from a RNN. USeful for debugging

    Strategy: Initialize sequence with 1,0,0 : i.e. end of stroke, no previously drawn data
    Encode the input text string with one-hot
    Pass the sequence and onehot to the RNN and obtain Mixture Gaussian parameters
    Then sample from this mixture gaussian
    and iterate until a threshold is reached

    Args:
        settings (ExperimentSettings): custom class to hold hyperparams
        rnn (nn.Model): the model we want to sample from
        truth_text (str): sentence to write

    Returns:
         plot_data (PlotData) custom class holding data arrays for plots
    """

    truth_onehot = np.zeros((1, len(truth_text), len(settings.d_char_to_idx.keys())), dtype=np.float32)
    for idx, char in enumerate(truth_text):
        truth_onehot[0, idx, settings.d_char_to_idx[char]] = 1.0

    # Reconstruct text from onehot
    reconstructed_text = ""
    for i in range(truth_onehot.shape[1]):
        char_idx = np.argmax(truth_onehot[0, i, :])
        char = settings.d_idx_to_char[char_idx]
        reconstructed_text += char

    # Sanity check to make sure there is no mixup between onehot and text
    assert truth_text == reconstructed_text

    # Prepare truth_onehot for RNN
    onehot = torch.from_numpy(truth_onehot)

    # Prepare input sequence
    sample = [1., 0., 0.]  # eos = 1 to tell the model to generate a new sample dx and dy initialized at 0
    sample = torch.Tensor(sample).view(1,1,3)  # format in (seq_len, batch_size, n_features) mode

    # Prepare kappa
    running_kappa = torch.zeros(1, rnn.n_window, 1)

    # Use GPU if needed
    if settings.use_cuda:
        sample = Variable(sample.cuda())
        onehot = Variable(onehot.cuda())
        running_kappa = Variable(running_kappa.cuda())
    else:
        sample = Variable(sample)
        onehot = Variable(onehot)
        running_kappa = Variable(running_kappa)

    # Prepare hidden var
    hidden = rnn.initHidden(sample.size(0))

    list_stroke = []
    list_density = []
    list_phi = []
    list_window = []
    list_kappa = []
    for i in range(settings.sampling_len):

        # Set training flag to false to indicate we are doing inference.
        mdnparams, e_logit, hidden = rnn(sample, hidden, onehot, training=False, running_kappa=running_kappa)

        # End condition when mean of densityian window is longer than the len of the one hot sequence
        mean_kappa = running_kappa.squeeze().mean().data.cpu().numpy()
        if mean_kappa + 1 > onehot.size(1):
            break

        # Sample a new data point
        params = stroke_sampling(settings, mdnparams, e_logit)

        # Roll out params
        x1 = params.x1
        x2 = params.x2
        eos = params.eos
        mu1 = params.mu1
        mu2 = params.mu2
        sigma1 = params.sigma1
        sigma2 = params.sigma2
        rho = params.rho

        # Get the tensors related to the attention window for plotting
        window = rnn.window.data.cpu().numpy()
        phi = rnn.phi.data.cpu().numpy()[0]
        kappa = rnn.new_kappa.data.cpu().numpy()

        # Updata sample and kappa
        sample = torch.cat([eos, x1, x2], -1).view(1,1,3)
        if settings.use_cuda:
            running_kappa = Variable(torch.from_numpy(kappa).cuda())
        else:
            running_kappa = Variable(torch.from_numpy(kappa))

        # Store parameters for plot
        list_stroke.append(sample.squeeze().data.cpu().numpy())
        list_window.append(window)
        list_phi.append(phi)
        list_kappa.append(kappa[0].T)
        list_density.append(np.hstack([mu1, mu2, sigma1, sigma2, rho]))

    arr_stroke = np.stack(list_stroke, axis=0)
    arr_window = np.concatenate(list_window, axis=0)
    arr_phi = np.stack(list_phi, axis=0)
    arr_density = np.stack(list_density, axis=0)
    arr_kappa = np.concatenate(list_kappa, axis=0)

    out = PlotData(stroke=arr_stroke, window=arr_window, phi=arr_phi,
                   density=arr_density, text=truth_text, kappa=arr_kappa)

    return out


def sample_test_MDN(settings, model, X):
    """Utility to sample an unconditional handwriting sequence from a RNN

    Strategy: Initialize sequence with 1,0,0 : i.e. end of stroke, no previously drawn data
    Pass the sequence to the RNN and obtain Mixture Gaussian parameters
    Then sample from this mixture gaussian
    and iterate until settings.sampling_len is reached

    Args:
        settings (ExperimentSettings): custom class to hold hyperparams
        model (nn.Model): the model we want to sample from
        X (np.array): the data at which to sample

    Returns:
        (np.array): sampled points
    """

    num_elem = X.shape[0]
    num_batches = num_elem / settings.batch_size
    list_batches = np.array_split(np.arange(num_elem), num_batches)

    list_samples = []

    for batch_idxs in list_batches:

        start, end = batch_idxs[0], batch_idxs[-1] + 1
        X_batch = X[start: end]
        X_var = Variable(torch.from_numpy(X_batch))

        # Forward pass
        mdnparams = model(X_var)

        params = stroke_sampling(settings, mdnparams)
        x1, x2 = params.x1, params.x2

        # Redefine sample
        sample = torch.cat([x1, x2], -1)

        list_samples.append(sample.data.cpu().numpy())

    return np.concatenate(list_samples, 0)


def stroke_sampling(settings, mdnparams, e_logit=None):
    """Utility to sample from MDN, possibly also sampling
    an end of stroke token

    Args:
        settings (ExperimentSettings): custom class to hold hyperparams
        mdnparams (MDNparams): custom namedtuple to hold MDN parameters
        e_logit (torch.Tensor): the predicted end of stroke token.

    Returns:
        params (SampleParams): custom class to hold stroke sampling results
    """

    # Roll out MDN params
    mu1 = mdnparams.mu1
    mu2 = mdnparams.mu2
    log_sigma1 = mdnparams.log_sigma1
    log_sigma2 = mdnparams.log_sigma2
    rho = mdnparams.rho
    pi_logit = mdnparams.pi_logit

    # To sample from mixture of gaussian, sample a component with bernoulli and weight pi
    idx = torch.multinomial(F.softmax(pi_logit * (1.0 + settings.bias)), 1)

    # Select the gaussian parameters corresponding to idxs
    mu1 = mu1.gather(1, idx)
    mu2 = mu2.gather(1, idx)
    sigma1 = (log_sigma1.gather(1, idx) - settings.bias).exp()
    sigma2 = (log_sigma2.gather(1, idx) - settings.bias).exp()
    rho = rho.gather(1, idx)

    # To sample from bivariate gaussian:
    # sample eps1, eps2 from N(0, 1) x N(01)
    # then x1 = sigma1 * eps1 + mu1
    # and x2 = sigma2 [rho * eps1 + sqrt(1-rho^2) * eps2] + mu2
    eps1 = torch.normal(mean=0., std=torch.ones(1)).view(1, -1)
    eps2 = torch.normal(mean=0., std=torch.ones(1)).view(1, -1)

    if settings.use_cuda:
        eps1 = eps1.cuda()
        eps2 = eps2.cuda()

    eps1 = Variable(eps1)
    eps2 = Variable(eps2)

    x1 = sigma1 * eps1 + mu1
    x2 = sigma2 * (rho * eps1 + torch.sqrt(1 - rho * rho) * eps2) + mu2

    # Move MDN params to cpu for use in plotting
    mu1 = mu1.data.cpu().numpy()[0]
    mu2 = mu2.data.cpu().numpy()[0]
    sigma1 = sigma1.data.cpu().numpy()[0]
    sigma2 = sigma2.data.cpu().numpy()[0]
    rho = rho.data.cpu().numpy()[0]

    if e_logit is None:

        params = SampleParams(x1=x1,
                              x2=x2,
                              eos=None,
                              mu1=mu1,
                              mu2=mu2,
                              sigma1=sigma1,
                              sigma2=sigma2,
                              rho=rho)

    else:
        # Now sample the eos tag with a simple bernoulli
        eos = torch.bernoulli(F.sigmoid(e_logit))

        params = SampleParams(x1=x1,
                              x2=x2,
                              eos=eos,
                              mu1=mu1,
                              mu2=mu2,
                              sigma1=sigma1,
                              sigma2=sigma2,
                              rho=rho)

    return params
