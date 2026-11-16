#[cfg(test)]
mod tests {
    #[test]
    fn workspace_smoke_test() {
        assert_eq!(
            microsoft_identity_native_primitives::microsoft_identity_native_abi_version(),
            1
        );
    }
}
