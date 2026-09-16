import { useState } from 'react';
import { UserPlus, Trash2 } from 'lucide-react';

export function UsersPage() {
  const [users] = useState([]);
  const [showAddUser, setShowAddUser] = useState(false);

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-2xl font-bold text-gray-800">User Management</h2>
        <button
          onClick={() => setShowAddUser(!showAddUser)}
          className="px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600 flex items-center gap-2"
        >
          <UserPlus className="w-4 h-4" />
          Add User
        </button>
      </div>

      {showAddUser && (
        <div className="bg-white p-6 rounded-lg shadow mb-6">
          <h3 className="font-semibold text-gray-800 mb-4">Add New User</h3>
          {/* Add user form will go here */}
          <p className="text-gray-500">Form coming soon...</p>
        </div>
      )}

      {users.length === 0 ? (
        <div className="bg-white p-12 rounded-lg shadow text-center">
          <p className="text-gray-500 text-lg">No users yet</p>
          <p className="text-gray-400 text-sm mt-2">
            Add the first user to get started
          </p>
        </div>
      ) : (
        <div className="bg-white rounded-lg shadow overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-sm font-semibold text-gray-800">
                  Name
                </th>
                <th className="px-6 py-3 text-left text-sm font-semibold text-gray-800">
                  Email
                </th>
                <th className="px-6 py-3 text-left text-sm font-semibold text-gray-800">
                  Role
                </th>
                <th className="px-6 py-3 text-right text-sm font-semibold text-gray-800">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((user: any) => (
                <tr key={user.id} className="border-t border-gray-200">
                  <td className="px-6 py-3 text-gray-800">{user.name}</td>
                  <td className="px-6 py-3 text-gray-600">{user.email}</td>
                  <td className="px-6 py-3 text-gray-600">{user.role}</td>
                  <td className="px-6 py-3 text-right">
                    <button className="text-red-500 hover:text-red-700">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
