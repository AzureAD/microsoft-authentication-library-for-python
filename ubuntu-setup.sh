# Derived from https://stackoverflow.microsoft.com/articles/285523#heading-configuring-linux-desktop-new-install

# Pre-Req for compliance policy - Configure minimum password strength
sudo apt install libpam-pwquality
# check that the pam_pwquality line in /etc/pam.d/common-password contains at least the required settings:
password    requisite           pam_pwquality.so retry=3 dcredit=-1 ocredit=-1 ucredit=-1 lcredit=-1 minlen=12

# Step 1 - Add the apt sources & install for Edge and Intune

# Install Curl
sudo apt install curl

# Install Microsoft's public key
curl -sSl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc

# Install the standard focal production repo
curl https://packages.microsoft.com/config/ubuntu/20.04/prod.list | sudo tee /etc/apt/sources.list.d/microsoft-ubuntu-focal-prod.list

# Install Edge's dev channel repo
sudo sh -c 'echo "deb [arch=amd64] https://packages.microsoft.com/repos/edge stable main" > /etc/apt/sources.list.d/microsoft-edge-dev.list'

sudo apt update

# Install Edge
sudo apt install microsoft-edge-dev

# Install Intune
sudo apt install intune-portal
