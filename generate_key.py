import secrets
import string
import base64
import hashlib
from cryptography.fernet import Fernet

def generate_secure_keys():
    """Generate secure encryption keys"""
    
    # Method 1: Using secrets module (Most Secure)
    def generate_random_string(length):
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    # Generate DB encryption key (64 characters recommended)
    db_key = generate_random_string(64)
    
    # Generate Fernet encryption key (32 characters, will be hashed to 32 bytes)
    fernet_key = generate_random_string(32)
    
    # Generate Fernet key from random bytes (Alternative method)
    fernet_key_bytes = Fernet.generate_key()  # This is already base64 encoded
    
    print("=" * 50)
    print("🔐 SECURE ENCRYPTION KEYS - SAVE THESE SAFELY!")
    print("=" * 50)
    print(f"\n📦 DATABASE ENCRYPTION KEY (DB_ENCRYPTION_KEY):")
    print(f"{db_key}")
    print(f"\n🔐 GENERAL ENCRYPTION KEY (ENCRYPTION_KEY):")
    print(f"Option 1 (String to be hashed): {fernet_key}")
    print(f"Option 2 (Direct Fernet key): {fernet_key_bytes.decode()}")
    print("\n⚠️ WARNING:")
    print("• Save these keys in a secure location")
    print("• Never share these keys")
    print("• Keep backup of these keys")
    print("• Use in .env file or environment variables")
    
    return db_key, fernet_key_bytes.decode()

# Run this once to generate keys
if __name__ == "__main__":
    generate_secure_keys()