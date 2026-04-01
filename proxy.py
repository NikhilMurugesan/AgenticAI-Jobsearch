def format_proxies(file_path):
    with open(file_path, 'r') as file:
        lines = file.readlines()

    # Clean and strip each line
    proxies = [line.strip() for line in lines if line.strip()]

    # Convert to required format
    formatted = "[" + ", ".join(f'"{proxy}"' for proxy in proxies) + "]"

    print(formatted)


# Example usage
format_proxies("proxies.txt")