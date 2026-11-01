use cng_core::DEFAULT_PROVIDER_NAME;

use crate::allocation;

pub const MAX_KEY_NAME: usize = 512;
pub const MAX_PROVIDER_NAME: usize = 512;
pub const MAX_STORE_URI: usize = 2048;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoreUri {
    pub key_name: String,
    pub provider_name: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StoreUriError {
    Invalid,
    Memory,
}

pub fn parse_store_uri(uri: &str) -> Result<StoreUri, StoreUriError> {
    if uri.is_empty() || uri.len() >= MAX_STORE_URI {
        return Err(StoreUriError::Invalid);
    }
    let (prefix, query) = uri.split_at_checked(7).ok_or(StoreUriError::Invalid)?;
    if !prefix.eq_ignore_ascii_case("cng://?") || query.is_empty() {
        return Err(StoreUriError::Invalid);
    }

    let mut key_name = None;
    let mut provider_name = None;
    for field in query.split('&') {
        if field.is_empty() || field.matches('=').count() != 1 {
            return Err(StoreUriError::Invalid);
        }
        let (name, encoded) = field.split_once('=').ok_or(StoreUriError::Invalid)?;
        let value = decode_value(encoded)?;
        match name {
            "key" if key_name.is_none() && value.len() < MAX_KEY_NAME => key_name = Some(value),
            "provider" if provider_name.is_none() && value.len() < MAX_PROVIDER_NAME => {
                provider_name = Some(value);
            }
            _ => return Err(StoreUriError::Invalid),
        }
    }

    let key_name = key_name.ok_or(StoreUriError::Invalid)?;
    let provider_name = match provider_name {
        Some(value) => value,
        None => allocation::try_copy_str(DEFAULT_PROVIDER_NAME).ok_or(StoreUriError::Memory)?,
    };
    Ok(StoreUri {
        key_name,
        provider_name,
    })
}

fn decode_value(encoded: &str) -> Result<String, StoreUriError> {
    if encoded.is_empty() {
        return Err(StoreUriError::Invalid);
    }
    let source = encoded.as_bytes();
    let mut decoded = Vec::new();
    decoded
        .try_reserve_exact(source.len())
        .map_err(|_| StoreUriError::Memory)?;
    let mut index = 0;
    while index < source.len() {
        let value = if source[index] == b'%' {
            let high = source
                .get(index + 1)
                .and_then(|value| hex(*value))
                .ok_or(StoreUriError::Invalid)?;
            let low = source
                .get(index + 2)
                .and_then(|value| hex(*value))
                .ok_or(StoreUriError::Invalid)?;
            index += 3;
            (high << 4) | low
        } else {
            let value = source[index];
            if !value.is_ascii_alphanumeric() && !matches!(value, b'-' | b'.' | b'_' | b'~') {
                return Err(StoreUriError::Invalid);
            }
            index += 1;
            value
        };
        if value == 0 || value < 0x20 || value == 0x7f {
            return Err(StoreUriError::Invalid);
        }
        decoded.push(value);
    }
    if decoded.is_empty() {
        return Err(StoreUriError::Invalid);
    }
    String::from_utf8(decoded).map_err(|_| StoreUriError::Invalid)
}

const fn hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_strict_encoded_names() {
        let parsed = parse_store_uri(
            "CNG://?key=client-%E6%B5%8B%E8%AF%95&provider=Microsoft%20Software%20Key%20Storage%20Provider",
        )
        .expect("valid URI");
        assert_eq!(parsed.key_name, "client-测试");
        assert_eq!(parsed.provider_name, DEFAULT_PROVIDER_NAME);
    }

    #[test]
    fn defaults_provider() {
        let parsed = parse_store_uri("cng://?key=test-key").expect("valid URI");
        assert_eq!(parsed.provider_name, DEFAULT_PROVIDER_NAME);
    }

    #[test]
    fn rejects_ambiguous_or_malformed_fields() {
        for uri in [
            "cng://?",
            "cng://?provider=x",
            "cng://?key=x&",
            "cng://?key=x&key=y",
            "cng://?Key=x",
            "cng://?key=x+y",
            "cng://?key=%",
            "cng://?key=%00",
            "cng://?key=x=1",
            "cng://?key=x&unknown=y",
        ] {
            assert!(parse_store_uri(uri).is_err(), "{uri}");
        }
    }
}
