# Multi-Tenant Chatbot API - Admin Guide

## 🔐 Access Control

- **Admin Only**: Create users, manage API keys, view all users
- **Users**: Access their own chatbot, upload documents, manage own API key
- **Data Isolation**: Each user's data is completely separate

---

## 👨‍💼 Admin Endpoints

### 1. Create New User (Admin Only)

```bash
curl -X POST http://localhost:8000/api/v1/users/create/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "customer1",
    "email": "customer1@example.com",
    "password": "SecurePassword123!",
    "password_confirm": "SecurePassword123!"
  }'
```

**Response:**
```json
{
  "id": 2,
  "username": "customer1",
  "email": "customer1@example.com",
  "api_key": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "is_active": true,
  "date_joined": "2026-05-01T12:00:00Z"
}
```

### 2. Get User's API Key (Admin Only)

```bash
curl -X GET http://localhost:8000/api/v1/users/{user_id}/api-key/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"
```

### 3. Regenerate User's API Key (Admin Only)

```bash
curl -X POST http://localhost:8000/api/v1/users/{user_id}/regenerate-api-key/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"
```

### 4. List All Users (Admin Only)

```bash
curl -X GET http://localhost:8000/api/v1/users/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"
```

### 5. Get/Update User Details (Admin Only)

```bash
curl -X GET http://localhost:8000/api/v1/users/{user_id}/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"

curl -X PATCH http://localhost:8000/api/v1/users/{user_id}/ \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

---

## 👤 User Endpoints

### 1. Login (Get JWT Token)

```bash
curl -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{
    "username": "customer1",
    "password": "SecurePassword123!"
  }'
```

**Response:**
```json
{
  "access": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "user": {
    "id": 2,
    "username": "customer1",
    "email": "customer1@example.com",
    "is_staff": false
  }
}
```

### 2. Get Your Own API Key

```bash
curl -X GET http://localhost:8000/api/v1/users/api-key/ \
  -H "Authorization: Bearer USER_JWT_TOKEN"
```

### 3. Regenerate Your Own API Key

```bash
curl -X POST http://localhost:8000/api/v1/users/regenerate-api-key/ \
  -H "Authorization: Bearer USER_JWT_TOKEN"
```

### 4. Get Your Profile

```bash
curl -X GET http://localhost:8000/api/v1/users/me/ \
  -H "Authorization: Bearer USER_JWT_TOKEN"
```

---

## 🤖 User Chatbot Endpoints

### Chat with Own Data (API Key Auth)

```bash
curl -X POST http://localhost:8000/api/v1/chat/ \
  -H "Authorization: ApiKey {USER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "ما هي خدماتكم؟",
    "language": "ar"
  }'
```

### Chat with Own Data (JWT Auth)

```bash
curl -X POST http://localhost:8000/api/v1/chat/ \
  -H "Authorization: Bearer {USER_JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What services do you offer?",
    "language": "en"
  }'
```

### Upload Document (Own Data Only)

```bash
curl -X POST http://localhost:8000/api/v1/documents/ \
  -H "Authorization: ApiKey {USER_API_KEY}" \
  -F "file=@company-data.pdf" \
  -F "is_active=true"
```

### View Own Documents

```bash
curl -X GET http://localhost:8000/api/v1/documents/ \
  -H "Authorization: ApiKey {USER_API_KEY}"
```

### Sync External API (Own Space)

```bash
curl -X POST http://localhost:8000/api/v1/sync-api-content/ \
  -H "Authorization: ApiKey {USER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "api_url": "https://your-api.com/articles/",
    "document_name": "Company Articles"
  }'
```

### Submit Feedback

```bash
curl -X POST http://localhost:8000/api/v1/chat/feedback/ \
  -H "Authorization: ApiKey {USER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is your name?",
    "answer": "I am a chatbot",
    "rating": "up",
    "comment": "Great answer"
  }'
```

---

## 🔑 Authentication Methods

### API Key Header
```
Authorization: ApiKey {API_KEY}
```

### JWT Bearer Token
```
Authorization: Bearer {JWT_TOKEN}
```

Both methods work for chatbot endpoints. Users can choose either.

---

## 📊 Data Isolation Example

**User A (customer1):**
- Uploads: "pricing.pdf", "company-info.docx"
- API searches only these 2 documents
- Cannot see User B's documents

**User B (customer2):**
- Uploads: "products.pdf", "faq.xlsx"
- API searches only these 2 documents
- Cannot see User A's documents

**Admin:**
- Can see all users
- Can regenerate any user's API key
- Can manage user accounts

---

## 🚀 Complete Workflow

### 1. Admin Creates User
```bash
Admin creates "customer1" with password → System generates API key
```

### 2. User Logs In
```bash
customer1 logs in → Gets JWT token + API key
```

### 3. User Uploads Data
```bash
customer1 uses API key → Uploads documents → Data tied to customer1
```

### 4. User Queries Own Data
```bash
customer1 uses API key → Asks questions → Only searches their documents
```

### 5. Admin Manages User
```bash
Admin uses JWT → Views customer1 → Can disable/regenerate API key
```

---

## ✅ Permissions Summary

| Endpoint | Admin | User | Public |
|----------|-------|------|--------|
| Create User | ✅ | ❌ | ❌ |
| List Users | ✅ | ❌ | ❌ |
| Get User Details | ✅ | ✅ (self) | ❌ |
| Get API Key (self) | ✅ | ✅ | ❌ |
| Get User's API Key | ✅ | ❌ | ❌ |
| Regenerate API Key | ✅ | ✅ (self) | ❌ |
| Chat | ✅ | ✅ | ❌ |
| Upload Document | ✅ | ✅ | ❌ |
| View Documents | ✅ (all) | ✅ (own) | ❌ |
| Sync API Content | ✅ | ✅ | ❌ |
| Submit Feedback | ✅ | ✅ | ❌ |

---

## 🔒 Admin Account

**Username:** `admin` (if exists) or create via Django shell:

```bash
python manage.py createsuperuser
```

**Then login:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/login/ \
  -d '{"username": "admin", "password": "..."}'
```

---

## 💡 Best Practices

✅ **Do:**
- Store API keys securely (env vars, secrets manager)
- Regenerate keys if compromised
- Use admin account only for management
- Regularly rotate API keys

❌ **Don't:**
- Commit API keys to git
- Share API keys between users
- Use public API keys in frontend
- Allow non-admins to create users
