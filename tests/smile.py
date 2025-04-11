#!/usr/bin/env python3
"""
MSAL Feature Test Runner
Interprets testcase file(s) to create and execute test cases using MSAL.

Initially created by the following prompt:
Write a python implementation that can read content from feature.yml, create variables whose names are defined in the "arrange" mapping's keys, and the variables' value are derived from the "arrange" mapping's value; interpret those value as if they are python snippet using MSAL library.
"""
import os
import sys
import logging
from contextlib import contextmanager
from typing import Dict, Any, List, Optional

import yaml
import msal
import requests


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SmileTestRunner:

    def __init__(self, testcase_url: str):
        self.testcase_url = testcase_url
        self.test_spec = None
        self.variables = {}

    def load_feature(self) -> Dict[str, Any]:
        """Load and validate the feature file."""
        try:
            with requests.get(self.testcase_url) as response:
                response.raise_for_status()
                self.test_spec = yaml.safe_load(response.text)

            # Basic validation
            if not isinstance(self.test_spec, dict):
                raise ValueError("Feature file must contain a valid YAML dictionary")

            if self.test_spec.get('type') != 'MSAL Test':
                raise ValueError("Feature file must have type 'MSAL Test'")

            return self.test_spec
        except Exception as e:
            logger.error(f"Error loading feature file: {str(e)}")
            sys.exit(1)

    @contextmanager
    def setup_environment(self):
        """Set up the environment variables specified in the feature file."""
        original_env = os.environ.copy()

        try:
            # Set environment variables
            if 'env' in self.test_spec and isinstance(self.test_spec['env'], dict):
                for key, value in self.test_spec['env'].items():
                    os.environ[key] = str(value)
                    logger.debug(f"Set environment variable {key}={value}")
            yield
        finally:
            # Restore original environment
            os.environ.clear()
            os.environ.update(original_env)

    def arrange(self):
        """Create variables based on the arrange section."""
        arrange_spec = self.test_spec.get('arrange', {})
        if not isinstance(arrange_spec, dict):
            raise ValueError("Arrange section must be a dictionary")
        for var_name, value_spec in arrange_spec.items():
            logger.debug(f"Creating variable '{var_name}' with {value_spec}")
            self.variables[var_name] = self._create_instance(value_spec)

    def _create_instance(self, spec: Dict[str, Any]) -> Any:
        """Create an instance based on the specification."""
        if not isinstance(spec, dict) or len(spec) != 1:
            raise ValueError(f"Invalid specification format: {spec}")

        class_name, params = next(iter(spec.items()))

        # Handle different MSAL classes
        if class_name == "ManagedIdentityClient":
            return msal.ManagedIdentityClient(http_client=requests.Session(), **params)
        elif class_name == "PublicClientApplication":
            return self._create_public_client_app(params)
        elif class_name == "ConfidentialClientApplication":
            return self._create_confidential_client_app(params)
        else:
            raise ValueError(f"Unsupported class: {class_name}")

    def _create_public_client_app(self, params: Dict[str, Any]) -> Any:
        """Create a PublicClientApplication instance."""
        if not params or 'client_id' not in params:
            raise ValueError("PublicClientApplication requires client_id")

        client_id = params.get('client_id')
        authority = params.get('authority')
        logger.debug(f"Creating PublicClientApplication with client_id: {client_id}, authority: {authority}")

        kwargs = {'client_id': client_id}
        if authority:
            kwargs['authority'] = authority

        return msal.PublicClientApplication(**kwargs)

    def _create_confidential_client_app(self, params: Dict[str, Any]) -> Any:
        """Create a ConfidentialClientApplication instance."""
        if not params or 'client_id' not in params or 'client_credential' not in params:
            raise ValueError("ConfidentialClientApplication requires client_id and client_credential")

        client_id = params.get('client_id')
        client_credential = params.get('client_credential')
        authority = params.get('authority')
        logger.debug(f"Creating ConfidentialClientApplication with client_id: {client_id}, authority: {authority}")

        kwargs = {'client_id': client_id, 'client_credential': client_credential}
        if authority:
            kwargs['authority'] = authority

        return msal.ConfidentialClientApplication(**kwargs)

    def execute_steps(self) -> bool:
        """Execute the test steps, returns whether all steps passed."""
        steps = self.test_spec.get('steps', [])
        passed = 0
        for i, step in enumerate(steps):
            logger.debug(f"Executing step {i+1}/{len(steps)}")
            if 'act' in step:
                result = self._execute_action(step['act'])
                if 'assert' in step:
                    if self._validate_assertions(result, step['assert']):
                        passed += 1
        logger.info(f"{passed} of {len(steps)} step(s) passed")
        return passed == len(steps)

    def _execute_action(self, act_spec: Dict[str, Any]) -> Any:
        """Execute an action based on the specification."""
        if not isinstance(act_spec, dict) or len(act_spec) != 1:
            raise ValueError(f"Invalid action specification: {act_spec}")

        action_str, params = next(iter(act_spec.items()))

        # Parse the action string (e.g., "app1.AcquireToken")
        parts = action_str.split('.')
        if len(parts) != 2:
            raise ValueError(f"Invalid action format: {action_str}")

        var_name = parts[0]
        method_name = {  # Map the method names in yml to actual method names
            "AcquireTokenForManagedIdentity": "acquire_token_for_client",
            }.get(parts[1])

        if method_name is None:
            raise ValueError(f"Unsupported method: {parts[1]}")

        if var_name not in self.variables:
            raise ValueError(f"Variable '{var_name}' not found")

        instance = self.variables[var_name]
        if not hasattr(instance, method_name):
            raise ValueError(f"Method '{method_name}' not found on {var_name}")

        method = getattr(instance, method_name)

        # Convert parameters to kwargs
        kwargs = params if params else {}

        # Execute the method with parameters
        logger.info(f"Calling {var_name}.{method_name} with {kwargs}")
        return method(**kwargs)

    def _validate_assertions(self, result: Any, assertions: Dict[str, Any]) -> bool:
        """Validate the assertions against the result."""
        logger.info(f"Validating assertions: {assertions}")
        for key, expected_value in assertions.items():
            if key not in result:
                logger.error(f"Assertion failed: '{key}' not found in result {result}")
                return False  # Failed
            actual_value = result[key]
            if actual_value != expected_value:
                logger.error(f"Assertion failed: expected {key}='{expected_value}', got '{actual_value}'")
                return False  # Failed
            else:
                logger.debug(f"Assertion passed: {key}='{actual_value}'")
        return True  # Passed

    def run(self) -> bool:
        """Run the entire test, returns whether it passed."""
        self.load_feature()

        with self.setup_environment():
            self.arrange()
            result = self.execute_steps()
            if result:
                logger.info(f"Test case {self.testcase_url} passed")
            else:
                logger.error(f"Test case {self.testcase_url} failed")
        return result


def run_testcases(testcases_url: str) -> bool:
    try:
        response = requests.get(testcases_url)
        response.raise_for_status()
        passed = 0
        testcases = response.json().get("testcases", [])
        for url in testcases:
            try:
                if SmileTestRunner(url).run():
                    passed += 1
            except Exception as e:
                logger.error(f"Test case {url} failed: {e}")
        (logger.info if passed == len(testcases) else logger.error)(
            f"Passed {passed} of {len(testcases)} test cases"
        )
        return passed == len(testcases)
    except requests.RequestException as e:
        logger.error(f"Failed to fetch test cases from {testcases_url}: {e}")
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MSAL Feature Test Runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--testcase", help="URL for a single test case")
    group.add_argument("--batch", help="URL for a batch of test cases in JSON format")
    args = parser.parse_args()

    if args.testcase:
        logger.setLevel(logging.DEBUG)
        success = SmileTestRunner(args.testcase).run()
    elif args.batch:
        logger.setLevel(logging.INFO)
        success = run_testcases(args.batch)

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
