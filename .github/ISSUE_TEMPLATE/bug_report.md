---
name: Bug report
about: Create a report to help us improve
title: "[Bug] "
labels: needs attention, untriaged
assignees: ''

---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Go to our [off-the-shelf samples](https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample) and pick one that is closest to your usage scenario. You should not need to modify the sample.
2. Follow the description of the sample, typically at the beginning of it, to prepare a `config.json` containing your test configurations
3. Run such sample, typically by `python sample.py config.json`
4. See the error
5. In this bug report, tell us the sample you choose, paste the content of the config.json with your test setup (which you can choose to skip your credentials, and/or mail it to our developer's email).

**Expected behavior**
A clear and concise description of what you expected to happen.

**What you see instead**
Paste the sample output, or add screenshots to help explain your problem.

**The MSAL Python version you are using**
Paste the output of this
`python -c "import msal; print(msal.__version__)"`

**Additional context**
Add any other context about the problem here.
