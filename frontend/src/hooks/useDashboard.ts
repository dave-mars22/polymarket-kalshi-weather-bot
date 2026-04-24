import { useQuery } from '@tanstack/react-query'
import { fetchDashboard } from '../api'

// Single source of truth for /api/dashboard polling. The 10 s refetch
// interval is deliberate (matches backend scheduler cadence); overriding
// here means mutations that invalidateQueries(['dashboard']) refire this
// hook instead of going through a private useQuery in App.tsx.
export function useDashboard() {
  return useQuery({
    queryKey: ['dashboard'],
    queryFn: fetchDashboard,
    refetchInterval: 10000,
  })
}
