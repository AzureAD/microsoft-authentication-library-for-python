use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use cng_core::{CngKey, DEFAULT_PROVIDER_NAME, delete_test_key, random_bytes};

fn owned_name() -> String {
    let mut random = [0_u8; 16];
    random_bytes(&mut random).expect("random multiprocess test key suffix");
    let suffix = random
        .iter()
        .map(|value| format!("{value:02x}"))
        .collect::<String>();
    format!(
        "openssl-cng-rust-test-process-race-{}-{suffix}",
        std::process::id()
    )
}

fn helper_output(output: Output, label: &str) -> Result<String, String> {
    if !output.status.success() {
        return Err(format!(
            "{label} failed with {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let output =
        String::from_utf8(output.stdout).map_err(|error| format!("{label} output: {error}"))?;
    let output = output.trim();
    if output.is_empty() {
        return Err(format!("{label} returned an empty public key"));
    }
    Ok(output.to_owned())
}

fn public_key(key: &CngKey) -> String {
    key.public_modulus()
        .iter()
        .chain(key.public_exponent())
        .map(|value| format!("{value:02x}"))
        .collect()
}

fn race_key_creation(name: &str) -> Result<(), String> {
    let start = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock precedes Unix epoch: {error}"))?
        .as_millis()
        .checked_add(2_000)
        .ok_or_else(|| "start time overflow".to_owned())?;
    let start = u64::try_from(start).map_err(|_| "start time does not fit in u64".to_owned())?;
    let helper = env!("CARGO_BIN_EXE_key-race-helper");
    let spawn = || {
        Command::new(helper)
            .arg(name)
            .arg(start.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
    };

    let first = spawn().map_err(|error| format!("spawn first helper: {error}"))?;
    let second = match spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = first.wait_with_output();
            return Err(format!("spawn second helper: {error}"));
        }
    };
    let first_output = first
        .wait_with_output()
        .map_err(|error| format!("wait for first helper: {error}"))?;
    let second_output = second
        .wait_with_output()
        .map_err(|error| format!("wait for second helper: {error}"))?;
    let first = helper_output(first_output, "first helper")?;
    let second = helper_output(second_output, "second helper")?;
    if first != second {
        return Err("helpers observed different public keys".to_owned());
    }

    let reopened = CngKey::open(DEFAULT_PROVIDER_NAME, name).map_err(|error| error.to_string())?;
    if first != public_key(&reopened) {
        return Err("helper output differs from the persisted public key".to_owned());
    }
    Ok(())
}

#[test]
fn multiprocess_open_or_create_converges_on_one_key() {
    let name = owned_name();
    delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("pre-clean multiprocess test key");

    let result = race_key_creation(&name);
    let cleanup = delete_test_key(DEFAULT_PROVIDER_NAME, &name);

    cleanup.expect("delete multiprocess test key");
    result.expect("multiprocess open-or-create");
    assert!(
        CngKey::open(DEFAULT_PROVIDER_NAME, &name).is_err(),
        "test-owned key must be deleted"
    );
}
