#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Extract Kubernetes connector information from kubeconfig.

This script reads your kubeconfig and extracts the information needed
to create a Kubernetes connector in MEHO.

Usage:
    python scripts/extract_k8s_connector_info.py [context_name]
    
    If context_name is not provided, uses current context.
    
Example:
    python scripts/extract_k8s_connector_info.py mirza
    python scripts/extract_k8s_connector_info.py 10.5.21.100
"""
import json
import os
import sys
import subprocess
from pathlib import Path


def get_kubeconfig_path():
    """Get kubeconfig path from environment or default."""
    return os.environ.get("KUBECONFIG", str(Path.home() / ".kube" / "config"))


def run_kubectl(args: list) -> str:
    """Run kubectl command and return output."""
    try:
        result = subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error running kubectl {' '.join(args)}: {e.stderr}")
        sys.exit(1)
    except FileNotFoundError:
        print("kubectl not found. Please install kubectl first.")
        sys.exit(1)


def get_contexts():
    """Get list of available contexts."""
    output = run_kubectl(["config", "get-contexts", "-o", "name"])
    return output.split("\n") if output else []


def get_current_context():
    """Get current context name."""
    return run_kubectl(["config", "current-context"])


def extract_connector_info(context_name: str = None):
    """Extract connector information for a given context."""
    
    # Get context to use
    if context_name:
        contexts = get_contexts()
        if context_name not in contexts:
            print(f"Context '{context_name}' not found. Available contexts:")
            for ctx in contexts:
                print(f"  - {ctx}")
            sys.exit(1)
    else:
        context_name = get_current_context()
    
    print(f"=" * 60)
    print(f"Extracting info for context: {context_name}")
    print(f"=" * 60)
    
    # Get cluster info
    cluster_name = run_kubectl([
        "config", "view", "--raw", "-o", 
        f"jsonpath={{.contexts[?(@.name==\"{context_name}\")].context.cluster}}"
    ])
    
    server_url = run_kubectl([
        "config", "view", "--raw", "-o",
        f"jsonpath={{.clusters[?(@.name==\"{cluster_name}\")].cluster.server}}"
    ])
    
    # Get user info
    user_name = run_kubectl([
        "config", "view", "--raw", "-o",
        f"jsonpath={{.contexts[?(@.name==\"{context_name}\")].context.user}}"
    ])
    
    # Try to get token (may not exist for all auth types)
    token = ""
    try:
        token = run_kubectl([
            "config", "view", "--raw", "-o",
            f"jsonpath={{.users[?(@.name==\"{user_name}\")].user.token}}"
        ])
    except:
        pass
    
    # Check for certificate auth
    client_cert = ""
    try:
        client_cert = run_kubectl([
            "config", "view", "--raw", "-o",
            f"jsonpath={{.users[?(@.name==\"{user_name}\")].user.client-certificate-data}}"
        ])
    except:
        pass
    
    # Get CA cert
    ca_cert = ""
    try:
        ca_cert = run_kubectl([
            "config", "view", "--raw", "-o",
            f"jsonpath={{.clusters[?(@.name==\"{cluster_name}\")].cluster.certificate-authority-data}}"
        ])
    except:
        pass
    
    # Determine auth type
    auth_type = "unknown"
    if token:
        auth_type = "bearer_token"
    elif client_cert:
        auth_type = "client_certificate"
    
    # Build OpenAPI spec URL
    openapi_url = f"{server_url}/openapi/v2"
    
    print()
    print("📋 MEHO Connector Configuration:")
    print("-" * 60)
    print()
    print(f"Name:           Kubernetes - {context_name}")
    print(f"Type:           kubernetes (or rest)")
    print(f"Base URL:       {server_url}")
    print(f"OpenAPI Spec:   {openapi_url}")
    print(f"Auth Type:      {auth_type}")
    print()
    
    if token:
        print("🔑 Bearer Token (first 50 chars):")
        print(f"   {token[:50]}..." if len(token) > 50 else f"   {token}")
        print()
    
    if ca_cert:
        print("🔒 CA Certificate: Present (base64 encoded)")
        print(f"   Length: {len(ca_cert)} chars")
        print()
    
    # Test API access
    print("🧪 Testing API access...")
    try:
        # Use kubectl to test
        run_kubectl(["--context", context_name, "get", "namespaces", "--no-headers"])
        print("   ✅ API accessible!")
    except:
        print("   ⚠️  API test failed (may need auth)")
    
    print()
    print("=" * 60)
    print("JSON Configuration for MEHO API:")
    print("=" * 60)
    
    config = {
        "name": f"Kubernetes - {context_name}",
        "connector_type": "rest",  # K8s uses REST API
        "base_url": server_url,
        "description": f"Kubernetes cluster: {context_name}",
        "auth_type": auth_type,
    }
    
    if token:
        config["auth_config"] = {
            "type": "bearer",
            "token": token
        }
    
    print()
    print(json.dumps(config, indent=2))
    print()
    
    # Also output curl command to test
    print("=" * 60)
    print("Test with curl:")
    print("=" * 60)
    print()
    if token:
        print(f'curl -k -H "Authorization: Bearer {token[:20]}..." \\')
        print(f'  "{openapi_url}" | head -100')
    else:
        print(f'curl -k "{openapi_url}" | head -100')
    print()
    
    return config


def main():
    context = sys.argv[1] if len(sys.argv) > 1 else None
    
    print()
    print("🔍 Available Kubernetes Contexts:")
    print("-" * 40)
    contexts = get_contexts()
    current = get_current_context()
    for ctx in contexts:
        marker = " ← current" if ctx == current else ""
        print(f"  • {ctx}{marker}")
    print()
    
    extract_connector_info(context)


if __name__ == "__main__":
    main()

