"""
CLI to run mock systems.

Usage:
    python -m meho_mock_systems --all
    python -m meho_mock_systems --crm --orders
    python -m meho_mock_systems --crm --port 8001
"""
import argparse
import uvicorn
import multiprocessing
import time


def run_crm(port: int = 8001):
    """Run CRM mock API"""
    from meho_mock_systems.crm_app import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_orders(port: int = 8002):
    """Run Orders mock API"""
    from meho_mock_systems.orders_app import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_trading(port: int = 8003):
    """Run Trading mock API"""
    from meho_mock_systems.trading_app import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_providerx(port: int = 8004):
    """Run ProviderX News mock API"""
    from meho_mock_systems.providerx_news_app import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main():
    parser = argparse.ArgumentParser(description="Run MEHO mock systems")
    
    parser.add_argument("--all", action="store_true", help="Run all mock systems")
    parser.add_argument("--crm", action="store_true", help="Run CRM mock")
    parser.add_argument("--orders", action="store_true", help="Run Orders mock")
    parser.add_argument("--trading", action="store_true", help="Run Trading mock")
    parser.add_argument("--providerx", action="store_true", help="Run ProviderX News mock")
    parser.add_argument("--port", type=int, help="Port (only for single service)")
    
    args = parser.parse_args()
    
    # Determine which services to run
    services = []
    
    if args.all:
        services = [
            ("CRM", run_crm, 8001),
            ("Orders", run_orders, 8002),
            ("Trading", run_trading, 8003),
            ("ProviderX News", run_providerx, 8004)
        ]
    else:
        if args.crm:
            services.append(("CRM", run_crm, args.port or 8001))
        if args.orders:
            services.append(("Orders", run_orders, args.port or 8002))
        if args.trading:
            services.append(("Trading", run_trading, args.port or 8003))
        if args.providerx:
            services.append(("ProviderX News", run_providerx, args.port or 8004))
    
    if not services:
        print("Error: No services specified. Use --all or specify individual services.")
        parser.print_help()
        return
    
    if len(services) > 1 and args.port:
        print("Warning: --port ignored when running multiple services")
    
    # Run services
    processes = []
    
    try:
        for name, run_func, port in services:
            print(f"Starting {name} on port {port}...")
            process = multiprocessing.Process(target=run_func, args=(port,))
            process.start()
            processes.append((name, process, port))
            time.sleep(0.5)  # Stagger startup
        
        print("\n" + "=" * 60)
        print("Mock systems running:")
        for name, _, port in processes:
            print(f"  - {name:20} http://localhost:{port}")
            print(f"    OpenAPI spec:      http://localhost:{port}/openapi.json")
        print("=" * 60)
        print("\nPress Ctrl+C to stop all services\n")
        
        # Wait for processes
        for _, process, _ in processes:
            process.join()
    
    except KeyboardInterrupt:
        print("\n\nStopping mock systems...")
        for name, process, _ in processes:
            print(f"  Stopping {name}...")
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
        print("All mock systems stopped.")


if __name__ == "__main__":
    main()

