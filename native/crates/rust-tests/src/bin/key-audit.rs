use cng_core::{DEFAULT_PROVIDER_NAME, project_test_keys};

fn main() {
    match project_test_keys(DEFAULT_PROVIDER_NAME) {
        Ok(keys) if keys.is_empty() => println!("No project-specific CNG test keys remain."),
        Ok(keys) => {
            eprintln!("Project-specific CNG test keys remain:");
            for key in keys {
                eprintln!("  {key}");
            }
            std::process::exit(1);
        }
        Err(error) => {
            eprintln!("Unable to enumerate project-specific CNG keys: {error}");
            std::process::exit(1);
        }
    }
}
