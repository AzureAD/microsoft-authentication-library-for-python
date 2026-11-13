use std::time::{Duration, SystemTime, UNIX_EPOCH};

use cng_core::{CngKey, DEFAULT_PROVIDER_NAME};

fn unix_time_millis() -> Result<u64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock precedes Unix epoch: {error}"))?;
    u64::try_from(elapsed.as_millis()).map_err(|_| "system time does not fit in u64".to_owned())
}

fn public_key(key: &CngKey) -> String {
    key.public_modulus()
        .iter()
        .chain(key.public_exponent())
        .map(|value| format!("{value:02x}"))
        .collect()
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let key_name = arguments
        .next()
        .ok_or_else(|| "missing key name".to_owned())?;
    let start_millis = arguments
        .next()
        .ok_or_else(|| "missing start time".to_owned())?
        .parse::<u64>()
        .map_err(|error| format!("invalid start time: {error}"))?;
    if arguments.next().is_some() {
        return Err("unexpected extra argument".to_owned());
    }

    let now = unix_time_millis()?;
    std::thread::sleep(Duration::from_millis(start_millis.saturating_sub(now)));
    let key = CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &key_name, 2_048)
        .map_err(|error| error.to_string())?;
    println!("{}", public_key(&key));
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
