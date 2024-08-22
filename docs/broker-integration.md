# macOS broker integration

This doc indicates how to use `msal` to communicate with the brokers on Windows and macOS, which provides more secure authentication and single sign on experiences.

### Manually testing/integration steps
1. If you are on a modern Windows device, broker WAM is already built-in;
   If you are on a mac device, install CP (Company Portal), login an account in CP and finish the MDM process.
2. Install MSAL Python from its latest `dev` branch:
   `pip install --force-reinstall "git+https://github.com/AzureAD/microsoft-authentication-library-for-python.git[broker]"`
3. (Optional) A proper version of `PyMsalRuntime` has already been installed by the previous command.
   But if you want to test a specific version of `PyMsalRuntime`,
   you shall manually install that version now.

### Verification msal and pymsalruntime works fine

Please refer to [broker-test.py](https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/dev/tests/broker-test.py) as a sample.

Run `python broker-test.py` which will go through several basic scenarios like pop, ssh-cert and ROPC. Please provide credentials in the broker prompt (for interactive calls) and terminal (for ROPC), monitor whether all the tests pass.
