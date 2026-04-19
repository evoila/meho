# MEHO Frontend

Modern React + TypeScript frontend for MEHO AI Assistant.

## Tech Stack

- **Build Tool**: Vite (fast HMR and builds)
- **Framework**: React 18 + TypeScript
- **Styling**: Tailwind CSS
- **Routing**: React Router v6
- **State Management**: TanStack Query (for API state)
- **HTTP Client**: Axios
- **Icons**: Lucide React

## Quick Start

### Development

```bash
# Install dependencies
npm install

# Start dev server
npm run dev

# Frontend will run at http://localhost:5173
```

### Build for Production

```bash
# Build
npm run build

# Preview production build
npm run preview
```

## Project Structure

```
meho_frontend/
├── src/
│   ├── lib/
│   │   ├── api-client.ts      # MEHO API client with auth
│   │   └── config.ts           # Environment configuration
│   ├── components/
│   │   └── Layout.tsx          # Main layout with navigation
│   ├── pages/
│   │   ├── ChatPage.tsx        # Chat interface
│   │   ├── WorkflowsPage.tsx   # Workflow management
│   │   ├── KnowledgePage.tsx   # Knowledge base
│   │   └── ConnectorsPage.tsx  # API connectors
│   ├── App.tsx                 # Main app with routing
│   ├── main.tsx                # Entry point
│   └── index.css               # Tailwind CSS
├── tailwind.config.js          # Tailwind configuration
├── postcss.config.js           # PostCSS configuration
└── vite.config.ts              # Vite configuration
```

## Environment Variables

Create a `.env.local` file:

```bash
# MEHO API URL
VITE_API_URL=http://localhost:8000

# Optional: Dev token for testing
VITE_DEV_TOKEN=your-jwt-token
```

## API Connection

The frontend connects to the MEHO API (BFF) at `http://localhost:8000` by default.

Make sure the API is running:
```bash
cd /Users/damirtopic/repos/damir-topic/MEHO.X
source .venv/bin/activate
python -m meho_api.service
```

## Current Status

**Task 18: COMPLETE** ✅

- ✅ Vite + React + TypeScript setup
- ✅ Tailwind CSS configured
- ✅ React Router v6 configured
- ✅ TanStack Query configured
- ✅ API client with auth headers
- ✅ Basic layout with navigation
- ✅ Placeholder pages for all routes

**Next Tasks:**
- Task 19: Frontend Authentication
- Task 20: Chat UI with streaming
- Task 21: Knowledge management UI
- Task 22: Connector management UI

## Development Notes

### Styling

We use Tailwind CSS utility classes. Common patterns:

```tsx
// Container
<div className="max-w-7xl mx-auto px-4">

// Card
<div className="bg-white rounded-lg shadow p-6">

// Button
<button className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">

// Input
<input className="px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500">
```

### API Usage

```tsx
import { getAPIClient } from './lib/api-client';
import { config } from './lib/config';

const apiClient = getAPIClient(config.apiURL);

// Set auth token
apiClient.setToken(yourJWTToken);

// Create workflow
const workflow = await apiClient.createWorkflow({ goal: 'Your goal' });

// Search knowledge
const results = await apiClient.searchKnowledge({ query: 'search term' });
```

### Using TanStack Query

```tsx
import { useQuery, useMutation } from '@tanstack/react-query';

// Fetch data
const { data, isLoading, error } = useQuery({
  queryKey: ['workflows'],
  queryFn: () => apiClient.listWorkflows(),
});

// Mutate data
const mutation = useMutation({
  mutationFn: (goal: string) => apiClient.createWorkflow({ goal }),
  onSuccess: () => {
    // Invalidate and refetch
    queryClient.invalidateQueries({ queryKey: ['workflows'] });
  },
});
```

## License

Part of the MEHO project.
